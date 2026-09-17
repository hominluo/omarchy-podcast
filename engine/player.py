"""mpv as the audio engine, driven over its JSON IPC socket.

mpv runs as its own process (`--input-ipc-server`) so that:

  * `mpv-mpris` publishes it on MPRIS, which is what makes the media keys,
    Omarchy's media widget and the OSD work without any code here;
  * the engine can restart (an upgrade, a crash) while the audio keeps
    playing — on reconnect the properties are re-observed and state re-derived;
  * a hostile stream that takes mpv down does not take the daemon down.

The client speaks the wire format directly: `{"command": [...],
"request_id": n}` out, responses and `property-change` events in. Every
property we care about is observed once; `MpvClient.props` is the latest
value of each.
"""

import asyncio
import json
import os
import subprocess
import time

from . import VERSION, fsio, log

LOG = log.get("player")

MPRIS_CANDIDATES = ("/etc/mpv/scripts/mpris.so", "/usr/lib/mpv-mpris/mpris.so")
OBSERVED = (
    "time-pos", "duration", "pause", "speed", "volume", "mute", "chapter", "chapter-list", "metadata",
    "media-title", "eof-reached", "idle-active", "paused-for-cache", "cache-buffering-state", "seekable",
    "path", "af", "user-data/skipsilence/enabled", "user-data/skipsilence/base_speed", "user-data/podcast",
    "core-idle", "demuxer-cache-duration",
)
CONNECT_BUDGET = 6.0
SPAWN_LIMIT = 5          # per minute
COMMAND_TIMEOUT = 6.0
READ_LIMIT = 16 * 1024 * 1024


class MpvError(Exception):
    pass


class MpvClient:
    def __init__(self, engine):
        self.engine = engine
        self.paths = engine.paths
        self.reader = None
        self.writer = None
        self.connected = False
        self.props = {}
        self.process = None
        self.state = "starting"       # starting | connected | unavailable
        self.on_property = None
        self.on_event = None
        self.on_connect = None
        self.on_disconnect = None
        self._pending = {}
        self._next_id = 1
        self._task = None
        self._stopping = False
        self._spawns = []
        self._mpris_missing = False

    # ---- lifecycle ---------------------------------------------------------

    async def start(self):
        self._task = asyncio.ensure_future(self._run())

    async def stop(self, restart=False, quit_mpv=True):
        self._stopping = True
        if self.connected and quit_mpv:
            try:
                await asyncio.wait_for(self.command("quit"), 2)
            except Exception:  # noqa: BLE001
                pass
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._close()
        if quit_mpv and self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _close(self):
        self.connected = False
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:  # noqa: BLE001
                pass
        self.reader = self.writer = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MpvError("mpv disconnected"))
        self._pending.clear()

    async def _run(self):
        while not self._stopping:
            # Only knock on the socket when an mpv we know about is alive to
            # answer it; otherwise start one straight away.
            connected = await self._connect() if self._inheritable() else False
            if not connected:
                if not self._spawn():
                    self._set_state("unavailable")
                    await asyncio.sleep(15)
                    continue
                connected = await self._connect()
                if not connected:
                    LOG.error("mpv started but its socket never answered")
                    self._set_state("unavailable")
                    await asyncio.sleep(5)
                    continue
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOG.exception("mpv session ended unexpectedly")
            self._close()
            self._set_state("starting")
            if self.on_disconnect:
                try:
                    self.on_disconnect()
                except Exception:  # noqa: BLE001
                    LOG.exception("on_disconnect failed")
            await asyncio.sleep(0.5)

    def _set_state(self, state):
        if state != self.state:
            self.state = state
            self.engine.update_state("engine", mpv=state)

    # ---- process -----------------------------------------------------------

    def argv(self):
        args = [
            "mpv", "--no-config", "--idle=yes", "--terminal=no", "--msg-level=all=error",
            "--input-ipc-server=" + self.paths.mpv_socket_path,
            "--video=no", "--audio-display=no", "--cover-art-auto=no",
            "--keep-open=yes", "--keep-open-pause=yes",
            "--audio-client-name=Podcast", "--ytdl=no", "--access-references=no", "--load-unsafe-playlists=no",
            "--user-agent=Omarchy-Podcast/%s" % VERSION,
            "--cache=yes", "--demuxer-max-bytes=64MiB", "--demuxer-max-back-bytes=32MiB", "--cache-secs=900",
            "--network-timeout=20",
            "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_on_network_error=1,reconnect_delay_max=10",
            "--hr-seek=yes", "--audio-pitch-correction=yes", "--volume-max=100",
        ]
        mpris = next((path for path in MPRIS_CANDIDATES if os.path.exists(path)), None)
        if mpris:
            args.append("--script=" + mpris)
        else:
            self._mpris_missing = True
        if os.path.exists(self.paths.skipsilence_script):
            args.append("--script=" + self.paths.skipsilence_script)
            args.append("--script-opts=skipsilence-enabled=no")
        return args

    def _spawn(self):
        cutoff = time.monotonic() - 60
        self._spawns = [stamp for stamp in self._spawns if stamp > cutoff]
        if len(self._spawns) >= SPAWN_LIMIT:
            LOG.error("mpv keeps dying; giving it a minute")
            return False
        self._kill_stale()
        argv = self.argv()
        try:
            self.process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True)
        except OSError as error:
            LOG.error("cannot start mpv: %s", error)
            self.engine.notice("error", "mpv is not installed; run `omarchy pkg add mpv mpv-mpris`", code="mpv-missing")
            return False
        self._spawns.append(time.monotonic())
        try:
            fsio.atomic_write(self.paths.mpv_pid_path, str(self.process.pid))
        except OSError:
            pass
        LOG.info("started mpv (pid %d)", self.process.pid)
        if self._mpris_missing:
            self.engine.notice("warn", "mpv-mpris is not installed, so media keys will not reach the player", code="mpris-missing")
        return True

    def _recorded_pid(self):
        """Pid of the mpv a previous daemon started, if that process is still
        ours (its command line names our socket)."""
        try:
            with open(self.paths.mpv_pid_path, "r", encoding="ascii") as handle:
                pid = int(handle.read().strip() or 0)
        except (OSError, ValueError):
            return 0
        if pid <= 0:
            return 0
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as handle:
                cmdline = handle.read()
        except OSError:
            return 0
        return pid if self.paths.mpv_socket_path.encode() in cmdline else 0

    def _inheritable(self):
        if not os.path.exists(self.paths.mpv_socket_path):
            return False
        if self.process is not None and self.process.poll() is None:
            return True
        return self._recorded_pid() > 0

    def _kill_stale(self):
        """An mpv from a previous daemon that no longer answers its socket."""
        pid = self._recorded_pid()
        if pid > 0:
            LOG.warning("killing unresponsive mpv (pid %d)", pid)
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        try:
            os.unlink(self.paths.mpv_socket_path)
        except OSError:
            pass

    # ---- connection --------------------------------------------------------

    async def _connect(self):
        deadline = time.monotonic() + CONNECT_BUDGET
        delay = 0.02
        while time.monotonic() < deadline and not self._stopping:
            try:
                # A chapter-list or metadata dump from a long file can run past
                # asyncio's 64 KiB default line limit.
                self.reader, self.writer = await asyncio.open_unix_connection(self.paths.mpv_socket_path, limit=READ_LIMIT)
                self.connected = True
                return True
            except (OSError, ConnectionError):
                if self.process is not None and self.process.poll() is not None:
                    return False
                await asyncio.sleep(delay)
                delay = min(0.25, delay * 2)
        return False

    async def _session(self):
        LOG.info("connected to mpv")
        self.props = {}
        for index, name in enumerate(OBSERVED, start=1):
            self._send({"command": ["observe_property", index, name]})
        self._send({"command": ["request_log_messages", "warn"]})
        self._set_state("connected")
        # on_connect issues commands whose replies only arrive once the read
        # loop below is running, so it must not be awaited here.
        if self.on_connect:
            asyncio.ensure_future(self._run_on_connect())
        while not self._stopping:
            try:
                line = await self.reader.readline()
            except (asyncio.LimitOverrunError, ValueError):
                LOG.warning("mpv sent a line over %d bytes; dropping it", READ_LIMIT)
                continue
            if not line:
                LOG.warning("mpv closed the connection")
                return
            try:
                message = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            self._dispatch(message)

    async def _run_on_connect(self):
        try:
            await self.on_connect()
        except Exception:  # noqa: BLE001
            LOG.exception("on_connect failed")

    def _dispatch(self, message):
        if "request_id" in message and "event" not in message:
            future = self._pending.pop(message.get("request_id"), None)
            if future is not None and not future.done():
                if message.get("error", "success") == "success":
                    future.set_result(message.get("data"))
                else:
                    future.set_exception(MpvError(message.get("error")))
            return
        event = message.get("event")
        if event == "property-change":
            name = message.get("name")
            value = message.get("data")
            self.props[name] = value
            if name not in ("time-pos", "demuxer-cache-duration", "cache-buffering-state"):
                LOG.debug("mpv %s = %s", name, str(value)[:120])
            if self.on_property:
                try:
                    self.on_property(name, value)
                except Exception:  # noqa: BLE001
                    LOG.exception("property handler failed for %s", name)
            return
        if event == "log-message":
            LOG.debug("mpv[%s] %s: %s", message.get("level"), message.get("prefix"), str(message.get("text", "")).strip())
            if self.on_event:
                try:
                    self.on_event(message)
                except Exception:  # noqa: BLE001
                    LOG.exception("event handler failed")
            return
        if self.on_event and event:
            try:
                self.on_event(message)
            except Exception:  # noqa: BLE001
                LOG.exception("event handler failed for %s", event)

    # ---- commands ----------------------------------------------------------

    def _send(self, payload):
        if self.writer is None:
            raise MpvError("mpv is not connected")
        self.writer.write((json.dumps(payload) + "\n").encode("utf-8"))

    async def command(self, *args, timeout=COMMAND_TIMEOUT):
        if not self.connected or self.writer is None:
            raise MpvError("mpv is not connected")
        request_id = self._next_id
        self._next_id += 1
        future = self.engine.loop.create_future()
        self._pending[request_id] = future
        self._send({"command": list(args), "request_id": request_id})
        try:
            await self.writer.drain()
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise MpvError("mpv did not answer %r" % (args[0],))

    def command_nowait(self, *args):
        if not self.connected or self.writer is None:
            return False
        try:
            self._send({"command": list(args)})
        except (OSError, MpvError):
            return False
        return True

    async def set_property(self, name, value):
        return await self.command("set_property", name, value)

    async def get_property(self, name):
        return await self.command("get_property", name)

    def get(self, name, default=None):
        value = self.props.get(name)
        return default if value is None else value
