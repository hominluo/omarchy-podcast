"""Unix-socket server speaking the NDJSON protocol.

One `Client` per connection. The first line has to be `hello`; after that any
registered command is accepted, responses go back with the caller's `id`, and
every connected client receives the events the engine broadcasts. Lines are
capped at protocol.MAX_LINE; a client that overruns it is disconnected rather
than allowed to grow the buffer without bound.
"""

import asyncio
import json
import math
import os
import socket
import stat
import struct

from . import PROTOCOL, VERSION, log
from . import protocol

LOG = log.get("server")

# The socket sits in a 0700 directory and is itself 0600, so only this user
# reaches it; the peer credential check below makes that explicit rather
# than inherited from directory permissions. Everything else bounds what one
# client may cost the daemon.
MAX_CLIENTS = 16
MAX_INFLIGHT = 32
WRITE_HIGH_WATER = 4 * 1024 * 1024


def _peer_uid(writer):
    """The uid at the other end of a unix socket (SO_PEERCRED)."""
    sock = writer.get_extra_info("socket")
    _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    return uid


class Client:
    _next_id = 1

    def __init__(self, server, reader, writer):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.name = ""
        self.plugin_version = ""
        self.greeted = False
        self.closed = False
        self.number = Client._next_id
        Client._next_id += 1
        self.tasks = set()            # in-flight requests
        self._drain_lock = asyncio.Lock()

    def send(self, payload):
        if self.closed:
            return
        # A client that stops reading must not grow our buffer without bound.
        try:
            backlog = self.writer.transport.get_write_buffer_size()
        except (AttributeError, RuntimeError):
            backlog = 0
        if backlog > WRITE_HIGH_WATER:
            LOG.warning("%r is not reading (%d bytes queued); dropping it", self, backlog)
            self.close()
            return
        try:
            self.writer.write((encode(payload) + "\n").encode("utf-8"))
        except (OSError, RuntimeError):
            self.closed = True

    async def drain(self):
        if self.closed:
            return
        try:
            async with self._drain_lock:
                await self.writer.drain()
        except (OSError, ConnectionError, RuntimeError):
            self.closed = True

    def respond(self, request_id, result):
        self.send({"id": request_id, "ok": True, "result": result if result is not None else {}})

    def fail(self, request_id, error):
        self.send({"id": request_id, "ok": False, "error": error.to_json()})

    def close(self):
        if self.closed:
            return
        self.closed = True
        # In-flight requests run to completion: a subscribe or refresh whose
        # requester went away (shell reload) must still finish. Replies to a
        # closed client are dropped by send().
        try:
            self.writer.close()
        except (OSError, RuntimeError):
            pass

    def __repr__(self):
        return "Client(%d, %s)" % (self.number, self.name or "?")


def _request_id(line):
    """The id of a request we are about to refuse, parsed only then."""
    try:
        message = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return None
    return message.get("id") if isinstance(message, dict) else None


def encode(payload):
    """Compact JSON that a strict parser accepts: NaN and infinities (a
    broken duration, a division somewhere) become null instead of poisoning
    the whole line."""
    try:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except ValueError:
        return json.dumps(_finite(payload), separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _finite(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


class Server:
    def __init__(self, engine, socket_path):
        self.engine = engine
        self.socket_path = socket_path
        self.clients = set()
        self._server = None
        self._seq = 0

    async def start(self):
        self._remove_stale_socket()
        self._server = await asyncio.start_unix_server(self._on_connect, path=self.socket_path, limit=protocol.MAX_LINE)
        os.chmod(self.socket_path, 0o600)
        LOG.info("listening on %s", self.socket_path)

    async def stop(self):
        for client in list(self.clients):
            for task in list(client.tasks):
                task.cancel()
            client.close()
        self.clients.clear()
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001 - shutting down regardless
                pass
            self._server = None
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

    def _remove_stale_socket(self):
        # We hold the daemon lock, so a socket file here belongs to a dead
        # instance and is safe to replace.
        try:
            if stat.S_ISSOCK(os.stat(self.socket_path).st_mode):
                os.unlink(self.socket_path)
        except OSError:
            pass

    # ---- broadcasting ------------------------------------------------------

    def broadcast(self, event, data):
        self._seq += 1
        payload = {"event": event, "seq": self._seq, "data": data}
        for client in list(self.clients):
            if client.greeted:
                client.send(payload)

    @property
    def client_count(self):
        return len([client for client in self.clients if client.greeted])

    # ---- connection handling -----------------------------------------------

    async def _on_connect(self, reader, writer):
        try:
            uid = _peer_uid(writer)
        except (OSError, AttributeError, struct.error, TypeError):
            uid = None                                  # fail closed
        if uid != os.geteuid() or len(self.clients) >= MAX_CLIENTS:
            LOG.warning("refusing connection: peer uid %s, %d clients connected", uid, len(self.clients))
            try:
                writer.close()
            except (OSError, RuntimeError):
                pass
            return
        client = Client(self, reader, writer)
        self.clients.add(client)
        LOG.debug("%r connected", client)
        try:
            await self._serve(client)
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception:  # noqa: BLE001 - one bad client must not take the server down
            LOG.exception("%r crashed the connection handler", client)
        finally:
            self.clients.discard(client)
            client.close()
            LOG.debug("%r disconnected", client)

    async def _serve(self, client):
        while not client.closed:
            try:
                raw = await client.reader.readline()
            except (asyncio.LimitOverrunError, ValueError):
                client.send({"id": None, "ok": False, "error": protocol.ProtocolError(
                    protocol.BAD_REQUEST, "line exceeds %d bytes" % protocol.MAX_LINE).to_json()})
                await client.drain()
                return
            except (ConnectionError, OSError):
                return
            if not raw:
                return
            line = raw.strip()
            if not line:
                continue
            if not client.greeted:
                # The handshake is sequential; everything after it may overlap
                # so a slow subscribe does not hold up a pause.
                await self._handle_line(client, line)
                await client.drain()
                continue
            if len(client.tasks) >= MAX_INFLIGHT:
                client.fail(_request_id(line), protocol.ProtocolError(
                    protocol.RATE_LIMITED, "too many requests in flight (%d)" % MAX_INFLIGHT))
                await client.drain()
                continue
            task = asyncio.ensure_future(self._request(client, line))
            client.tasks.add(task)
            task.add_done_callback(client.tasks.discard)

    async def _request(self, client, line):
        try:
            await self._handle_line(client, line)
            await client.drain()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            LOG.exception("%r request handler crashed", client)

    async def _handle_line(self, client, line):
        try:
            message = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            client.fail(None, protocol.ProtocolError(protocol.BAD_REQUEST, "malformed JSON"))
            return
        if not isinstance(message, dict):
            client.fail(None, protocol.ProtocolError(protocol.BAD_REQUEST, "request must be an object"))
            return

        request_id = message.get("id")
        name = message.get("cmd")

        if not client.greeted:
            if name != "hello":
                client.fail(request_id, protocol.ProtocolError(protocol.BAD_REQUEST, "say hello first"))
                client.close()
                return
            await self._hello(client, request_id, message)
            return

        try:
            cmd = protocol.lookup(name)
            args = protocol.parse_args(cmd, message)
            if cmd.is_coroutine:
                result = await cmd.handler(self.engine, client, **args)
            else:
                result = cmd.handler(self.engine, client, **args)
        except asyncio.CancelledError:
            raise
        except protocol.ProtocolError as error:
            if request_id is not None:
                client.fail(request_id, error)
            else:
                # Fire-and-forget request: the failure still deserves a face.
                self.engine.notice("warn", error.message, code=error.code)
            return
        except Exception as error:  # noqa: BLE001 - report, never crash the server
            LOG.exception("command %r failed", name)
            message = "%s: %s" % (type(error).__name__, error)
            if request_id is not None:
                client.fail(request_id, protocol.ProtocolError(protocol.INTERNAL, message))
            else:
                self.engine.notice("error", "%s failed: %s" % (name, message), code=protocol.INTERNAL)
            return
        if request_id is not None:
            client.respond(request_id, result)

    async def _hello(self, client, request_id, message):
        wanted = message.get("protocol")
        try:
            wanted = int(wanted)
        except (TypeError, ValueError):
            wanted = None
        client.name = str(message.get("client") or "client")
        client.plugin_version = str(message.get("pluginVersion") or "")
        if wanted != PROTOCOL:
            client.fail(request_id, protocol.ProtocolError(
                protocol.CONFLICT, "protocol %s not supported (daemon speaks %d)" % (wanted, PROTOCOL), protocol=PROTOCOL))
            await client.drain()
            client.close()
            # A plugin speaking a newer protocol is the cue to restart into
            # its code, or the two would never meet.
            self.engine.on_client_hello(client)
            return
        client.greeted = True
        client.respond(request_id, {
            "protocol": PROTOCOL,
            "engineVersion": VERSION,
            "pid": os.getpid(),
            "startedAt": self.engine.started_at,
        })
        # The snapshot is sent to this client only; the seq counter keeps
        # counting so the client can order it against later broadcasts.
        self._seq += 1
        client.send({"event": "snapshot", "seq": self._seq, "data": self.engine.snapshot()})
        LOG.info("%r said hello (plugin %s)", client, client.plugin_version or "?")
        self.engine.on_client_hello(client)
