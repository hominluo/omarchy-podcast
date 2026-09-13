"""Command line entry: `podcastd.py serve|stop|restart|status|call ...`.

`serve` is what the shell runs. It takes the single-instance lock, detaches
from whatever spawned it, and runs the engine until asked to stop; when the
engine asks for a restart it re-execs the same launcher so the mpv child keeps
playing across an engine upgrade.

The other verbs are thin clients over the same socket, handy from a terminal
and used by ./check for smoke tests.
"""

import argparse
import asyncio
import errno
import fcntl
import json
import os
import signal
import socket
import sys
import time

from . import PROTOCOL, VERSION, log
from .config import Paths

LOCK_FD_ENV = "OMARCHY_PODCAST_LOCK_FD"


def main(argv, launcher=None):
    parser = argparse.ArgumentParser(prog="podcastd", description="Omarchy-Podcast engine")
    sub = parser.add_subparsers(dest="verb")

    serve = sub.add_parser("serve", help="run the daemon (what the shell does)")
    serve.add_argument("--foreground", action="store_true", help="stay attached; log to stderr as well")
    serve.add_argument("--debug", action="store_true", help="wire-level logging")
    serve.add_argument("--replace", action="store_true", help="stop a running instance first")

    sub.add_parser("stop", help="ask the running daemon to exit")
    sub.add_parser("restart", help="ask the running daemon to re-exec itself")
    sub.add_parser("status", help="print the running daemon's status")
    sub.add_parser("commands", help="list the commands the daemon understands")

    call = sub.add_parser("call", help="send one command: call <cmd> [--key value ...]")
    call.add_argument("cmd")
    call.add_argument("params", nargs=argparse.REMAINDER, help="--key value pairs; values are parsed as JSON when possible")

    args = parser.parse_args(argv)
    plugin_dir = os.path.dirname(os.path.abspath(launcher)) if launcher else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = Paths(plugin_dir)

    if args.verb in (None, "serve"):
        foreground = getattr(args, "foreground", False)
        debug = getattr(args, "debug", False)
        replace = getattr(args, "replace", False)
        return run_serve(paths, launcher, foreground=foreground, debug=debug, replace=replace)
    if args.verb == "stop":
        return print_result(request(paths, {"cmd": "shutdown"}))
    if args.verb == "restart":
        return print_result(request(paths, {"cmd": "restart"}))
    if args.verb == "status":
        return print_result(request(paths, {"cmd": "status"}))
    if args.verb == "commands":
        return print_result(request(paths, {"cmd": "commands"}))
    if args.verb == "call":
        message = {"cmd": args.cmd}
        message.update(parse_params(args.params))
        return print_result(request(paths, message))
    parser.print_help()
    return 2


# ------------------------------------------------------------------ serve

def run_serve(paths, launcher, foreground=False, debug=False, replace=False):
    paths.ensure()
    log.setup(paths.log_path, debug=debug, foreground=foreground)
    LOG = log.get("cli")

    lock = acquire_lock(paths, replace=replace)
    if lock is None:
        # Another instance holds the lock. That is the normal outcome when the
        # shell races its own reconnect timer; nothing to report.
        return 0

    if not foreground:
        detach()

    from .engine import Engine
    from . import subsystems

    engine = Engine(paths, launcher=launcher, foreground=foreground, debug=debug)
    subsystems.attach(engine)

    try:
        restart = asyncio.run(engine.run())
    except KeyboardInterrupt:
        restart = False
    except Exception:  # noqa: BLE001 - log the crash, exit non-zero
        LOG.exception("engine crashed")
        release_lock(lock)
        return 1

    if restart and launcher:
        # The lock travels through exec so no second instance can slip in
        # between the old process and the new one.
        os.set_inheritable(lock, True)
        os.environ[LOCK_FD_ENV] = str(lock)
        engine.exec_restart()
    release_lock(lock)
    return 0


def acquire_lock(paths, replace=False):
    inherited = os.environ.pop(LOCK_FD_ENV, None)
    if inherited:
        try:
            fd = int(inherited)
            if os.fstat(fd).st_ino == os.stat(paths.lock_path).st_ino:
                os.set_inheritable(fd, False)
                os.ftruncate(fd, 0)
                os.pwrite(fd, str(os.getpid()).encode("ascii"), 0)
                return fd
            os.close(fd)
        except (ValueError, OSError):
            pass
    fd = os.open(paths.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    for attempt in range(60):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            if not replace:
                os.close(fd)
                return None
            if attempt == 0:
                holder = read_pid(paths)
                if holder:
                    try:
                        os.kill(holder, signal.SIGTERM)
                    except OSError:
                        pass
            time.sleep(0.1)
            continue
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        return fd
    os.close(fd)
    return None


def release_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except OSError:
        pass


def read_pid(paths):
    try:
        with open(paths.info_path, "r", encoding="utf-8") as handle:
            return int(json.load(handle).get("pid") or 0)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with open(paths.lock_path, "r", encoding="utf-8") as handle:
            return int(handle.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def detach():
    """Become our own session so a shell restart cannot take us down, and let
    go of the spawner's stdio. Quickshell already starts us detached, so every
    step here tolerates having been done already."""
    try:
        if os.getpgrp() != os.getpid():
            os.setsid()
    except OSError:
        pass
    try:
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            try:
                os.dup2(null, fd)
            except OSError:
                pass
        if null > 2:
            os.close(null)
    except OSError:
        pass
    try:
        os.chdir("/")
    except OSError:
        pass


# ----------------------------------------------------------------- client

def request(paths, message, timeout=15.0):
    """One-shot client: hello, the command, return (ok, payload)."""
    if not os.path.exists(paths.socket_path):
        return False, {"code": "unavailable", "message": "daemon is not running (%s missing)" % paths.socket_path}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(paths.socket_path)
    except OSError as error:
        return False, {"code": "unavailable", "message": "cannot connect: %s" % error}
    try:
        stream = sock.makefile("rwb")
        hello = {"id": 1, "cmd": "hello", "client": "cli", "protocol": PROTOCOL, "pluginVersion": VERSION}
        stream.write((json.dumps(hello) + "\n").encode("utf-8"))
        payload = dict(message)
        payload["id"] = 2
        stream.write((json.dumps(payload) + "\n").encode("utf-8"))
        stream.flush()
        # hello response, snapshot event, then ours (events may interleave).
        while True:
            line = stream.readline()
            if not line:
                return False, {"code": "unavailable", "message": "connection closed"}
            reply = json.loads(line.decode("utf-8", "replace"))
            if reply.get("id") == 1 and not reply.get("ok", True):
                return False, reply.get("error", {})
            if reply.get("id") == 2:
                if reply.get("ok"):
                    return True, reply.get("result", {})
                return False, reply.get("error", {})
    except (OSError, ValueError) as error:
        return False, {"code": "unavailable", "message": str(error)}
    finally:
        sock.close()


def parse_params(tokens):
    """`--episodeId 5 --where next` -> {"episodeId": 5, "where": "next"}."""
    params = {}
    key = None
    for token in tokens:
        if token.startswith("--"):
            if key is not None:
                params[key] = True
            key = token[2:]
            continue
        if key is None:
            continue
        try:
            params[key] = json.loads(token)
        except ValueError:
            params[key] = token
        key = None
    if key is not None:
        params[key] = True
    return params


def print_result(outcome):
    ok, payload = outcome
    json.dump(payload, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if ok else 1
