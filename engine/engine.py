"""Composition root: owns the event loop, the store, the server and every
subsystem, and holds the state object that Service.qml mirrors.

State is a flat dict of named slices (`player`, `queue`, `library`, ...).
`set_state(key, value)` replaces one slice and broadcasts it as an event of
the same name; `snapshot()` hands a new client all of them at once. Anything
too large to live in state (episode pages, transcripts, search results) is
request/response instead.
"""

import asyncio
import concurrent.futures
import json
import os
import signal
import sys
import time

from . import PLUGIN_ID, PROTOCOL, VERSION, log, protocol
from .config import Paths, Settings, load_credentials, manifest_defaults, manifest_version, save_credentials
from .server import Server
from .store import Store

LOG = log.get("engine")

# Slices every client can expect in a snapshot, with their empty values.
EMPTY_STATE = {
    "player": {
        "episodeId": None, "pos": 0.0, "duration": 0.0, "paused": True, "idle": True,
        "buffering": False, "speed": 1.0, "baseSpeed": 1.0, "volume": 100, "mute": False,
        "chapter": -1, "skipSilence": False, "voiceBoost": False, "sleep": {"mode": "off", "endsAt": 0},
    },
    "nowPlaying": None,
    "queue": [],
    "library": [],
    "inbox": {"count": 0, "items": []},
    "downloads": [],
    "jobs": {"refreshing": False, "refreshDone": 0, "refreshTotal": 0, "transcribing": [], "modelDownload": None},
    "engine": {"version": VERSION, "mpv": "starting", "whisper": {"available": False, "gpu": False, "model": "", "modelPresent": False},
               "providers": {"itunes": True, "podcastindex": False}},
    "config": {},
    "sync": {"provider": "none", "lastSyncAt": 0, "lastError": "", "pending": 0, "syncing": False},
}


class Engine:
    def __init__(self, paths, launcher=None, foreground=False, debug=False):
        self.paths = paths
        self.launcher = launcher
        self.foreground = foreground
        self.debug = debug
        self.settings = Settings(manifest_defaults(paths))
        self.credentials = {}
        self.store = None
        self.server = Server(self, paths.socket_path)
        self.loop = None
        self.started_at = int(time.time())
        self.state = {key: _copy(value) for key, value in EMPTY_STATE.items()}
        self.subsystems = []
        self._stop = None
        self._restart = False
        self._quit_mpv = True
        self._last_restart_check = 0.0
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="podcast-io")
        self._settings_listeners = []

    # ---- lifecycle ---------------------------------------------------------

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self.loop.set_exception_handler(self._loop_exception)
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self.loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            self.loop.add_signal_handler(signal.SIGHUP, lambda: None)
        except (NotImplementedError, RuntimeError):
            pass

        self.store = Store(self.paths.db_path).open()
        self.credentials = load_credentials(self.paths)
        self._load_persisted()
        self.state["config"] = self.settings.as_dict()
        self._write_info()
        await self.server.start()

        for subsystem in self.subsystems:
            try:
                await _maybe_await(subsystem.start())
            except Exception:  # noqa: BLE001 - a broken subsystem must not stop the rest
                LOG.exception("subsystem %s failed to start", type(subsystem).__name__)

        LOG.info("engine %s ready (pid %d)", VERSION, os.getpid())
        await self._stop.wait()

        for subsystem in reversed(self.subsystems):
            try:
                await _maybe_await(subsystem.stop(restart=self._restart, quit_mpv=self._quit_mpv))
            except Exception:  # noqa: BLE001
                LOG.exception("subsystem %s failed to stop", type(subsystem).__name__)
        await self.server.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.store.close()
        try:
            os.unlink(self.paths.info_path)
        except OSError:
            pass
        return self._restart

    def request_stop(self, quit_mpv=True):
        self._quit_mpv = quit_mpv
        if self._stop is not None and not self._stop.is_set():
            LOG.info("stopping")
            self._stop.set()

    def request_restart(self):
        if not self.launcher:
            LOG.warning("restart requested but no launcher path is known; stopping instead")
            self.request_stop(quit_mpv=False)
            return
        self._restart = True
        self.request_stop(quit_mpv=False)

    def exec_restart(self):
        """Replace this process with a fresh interpreter on the same launcher.
        Called by cli after run() returns True; the lock fd is close-on-exec so
        the new process can take it."""
        argv = [sys.executable, self.launcher, "serve"]
        if self.foreground:
            argv.append("--foreground")
        if self.debug:
            argv.append("--debug")
        LOG.info("re-executing: %s", " ".join(argv))
        os.execv(sys.executable, argv)

    def _loop_exception(self, loop, context):
        message = context.get("message") or "event loop error"
        error = context.get("exception")
        if error is not None:
            LOG.error("%s: %r", message, error, exc_info=error)
        else:
            LOG.error("%s", message)

    def _write_info(self):
        try:
            with open(self.paths.info_path, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "version": VERSION, "protocol": PROTOCOL,
                           "startedAt": self.started_at, "socket": self.paths.socket_path}, handle)
        except OSError:
            pass

    def _load_persisted(self):
        player = self.state["player"]
        player["volume"] = int(self.store.get_setting("volume", 100))
        player["speed"] = float(self.store.get_setting("speed", self.settings.defaultSpeed))
        player["baseSpeed"] = player["speed"]
        player["skipSilence"] = bool(self.store.get_setting("skipSilence", False))
        player["voiceBoost"] = bool(self.store.get_setting("voiceBoost", False))

    # ---- state and events --------------------------------------------------

    def snapshot(self):
        return {key: _copy(value) for key, value in self.state.items()}

    def set_state(self, key, value):
        self.state[key] = value
        self.emit(key, value)

    def update_state(self, key, **fields):
        """Merge fields into a dict slice and broadcast it."""
        current = self.state.get(key)
        if not isinstance(current, dict):
            current = {}
        merged = dict(current)
        merged.update(fields)
        self.set_state(key, merged)
        return merged

    def emit(self, event, data):
        if self.server is not None:
            self.server.broadcast(event, data)

    def notice(self, level, text, code=None, episode_id=None):
        payload = {"level": level, "text": str(text)}
        if code:
            payload["code"] = code
        if episode_id is not None:
            payload["episodeId"] = episode_id
        getattr(LOG, "warning" if level in ("warn", "error") else "info")("notice: %s", text)
        self.emit("notice", payload)

    # ---- threads -----------------------------------------------------------

    def call_soon(self, fn, *args):
        """Schedule `fn(*args)` on the loop thread from anywhere."""
        if self.loop is None or self.loop.is_closed():
            return
        self.loop.call_soon_threadsafe(fn, *args)

    def run_in_thread(self, fn, *args):
        """Run blocking `fn(*args)` in the shared pool; returns an asyncio future."""
        return self.loop.run_in_executor(self._executor, fn, *args)

    # ---- settings ----------------------------------------------------------

    def on_settings_changed(self, listener):
        self._settings_listeners.append(listener)

    def apply_settings(self, values, locale=None, plugin_version=None):
        changed = self.settings.update(values, locale)
        self.set_state("config", self.settings.as_dict())
        if changed:
            LOG.info("settings updated")
            for listener in list(self._settings_listeners):
                try:
                    listener()
                except Exception:  # noqa: BLE001
                    LOG.exception("settings listener failed")
        if plugin_version:
            self._check_version(plugin_version)
        return changed

    def set_credentials(self, provider, values):
        current = dict(self.credentials)
        clean = {str(key): str(value) for key, value in values.items() if value is not None}
        if any(clean.values()):
            current[provider] = clean
        else:
            current.pop(provider, None)
        save_credentials(self.paths, current)
        self.credentials = current
        for listener in list(self._settings_listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                LOG.exception("settings listener failed")

    def credential(self, provider, key, default=""):
        block = self.credentials.get(provider)
        if isinstance(block, dict):
            return str(block.get(key, default) or default)
        return default

    # ---- clients -----------------------------------------------------------

    def on_client_hello(self, client):
        if client.plugin_version:
            self._check_version(client.plugin_version)

    def _check_version(self, plugin_version):
        """A newer plugin folder than the code in memory: restart into it. The
        manifest on disk has to agree with the client, or a half-updated folder
        could restart us in a loop."""
        if plugin_version == VERSION or self._restart:
            return
        now = time.monotonic()
        if now - self._last_restart_check < 30:
            return
        self._last_restart_check = now
        on_disk = manifest_version(self.paths)
        if on_disk and on_disk == plugin_version:
            LOG.info("plugin is %s but engine is %s; restarting into the new code", plugin_version, VERSION)
            self.request_restart()

    # ---- diagnostics -------------------------------------------------------

    def status(self):
        return {
            "version": VERSION,
            "pluginId": PLUGIN_ID,
            "pid": os.getpid(),
            "startedAt": self.started_at,
            "uptime": int(time.time()) - self.started_at,
            "clients": self.server.client_count,
            "socket": self.paths.socket_path,
            "db": self.paths.db_path,
            "log": self.paths.log_path,
            "counts": self.store.counts() if self.store else {},
            "engine": self.state.get("engine"),
            "settings": self.settings.as_dict(),
        }


def _copy(value):
    if isinstance(value, dict):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy(item) for item in value]
    return value


async def _maybe_await(result):
    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
        return await result
    return result


# ---------------------------------------------------------------- commands

A = protocol.Arg


@protocol.command("ping", "Liveness check")
def cmd_ping(engine, client):
    return {"pong": True, "time": int(time.time())}


@protocol.command("status", "Daemon status and counters")
def cmd_status(engine, client):
    return engine.status()


@protocol.command("configure", "Push the effective shell.json settings",
                  settings=A(dict), locale=A(str, required=False, default=""), pluginVersion=A(str, required=False, default=""))
def cmd_configure(engine, client, settings, locale, pluginVersion):
    changed = engine.apply_settings(settings, locale or None, pluginVersion or None)
    return {"changed": changed, "config": engine.settings.as_dict()}


@protocol.command("get-config", "Effective settings")
def cmd_get_config(engine, client):
    return {"config": engine.settings.as_dict(), "locale": engine.settings.locale}


@protocol.command("set-credentials", "Store a secret outside shell.json",
                  provider=A(str, choices=["podcastindex", "sync"]), values=A(dict))
def cmd_set_credentials(engine, client, provider, values):
    engine.set_credentials(provider, values)
    return {"providers": sorted(engine.credentials.keys())}


@protocol.command("log-tail", "Last lines of the daemon log", lines=A(int, required=False, default=100, minimum=1, maximum=2000))
def cmd_log_tail(engine, client, lines):
    return {"lines": log.tail(engine.paths.log_path, lines)}


@protocol.command("commands", "List every command the daemon understands")
def cmd_commands(engine, client):
    return {"commands": protocol.describe()}


@protocol.command("shutdown", "Stop the daemon", quitMpv=A(bool, required=False, default=True))
def cmd_shutdown(engine, client, quitMpv):
    engine.loop.call_later(0.05, engine.request_stop, quitMpv)
    return {"stopping": True}


@protocol.command("restart", "Re-exec the daemon; playback keeps going")
def cmd_restart(engine, client):
    engine.loop.call_later(0.05, engine.request_restart)
    return {"restarting": True}
