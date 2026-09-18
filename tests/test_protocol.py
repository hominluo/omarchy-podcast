import asyncio
import json
import os
import unittest

from engine import PROTOCOL, VERSION, protocol
from tests.fakes import EngineHarness as _Harness

class EngineHarness(_Harness):
    async def connect(self):
        reader, writer = await asyncio.open_unix_connection(self.paths.socket_path)
        return Conn(reader, writer)


class Conn:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    async def send(self, **message):
        self.writer.write((json.dumps(message) + "\n").encode())
        await self.writer.drain()

    async def refused(self):
        """True when the server dropped this connection without a word."""
        try:
            await self.send(id=1, cmd="hello", client="test", protocol=PROTOCOL, pluginVersion=VERSION)
            return await self.recv() is None
        except (ConnectionError, OSError):
            return True

    async def send_raw(self, data):
        self.writer.write(data)
        await self.writer.drain()

    async def recv(self):
        line = await asyncio.wait_for(self.reader.readline(), 5)
        if not line:
            return None
        return json.loads(line)

    async def hello(self, **extra):
        payload = {"id": 1, "cmd": "hello", "client": "test", "protocol": PROTOCOL, "pluginVersion": VERSION}
        payload.update(extra)
        await self.send(**payload)
        reply = await self.recv()
        snapshot = await self.recv() if reply.get("ok") else None
        return reply, snapshot

    def close(self):
        self.writer.close()


def run(coro):
    return asyncio.run(coro)


class ProtocolTest(unittest.TestCase):
    def test_hello_then_snapshot(self):
        async def scenario():
            async with EngineHarness() as h:
                conn = await h.connect()
                reply, snapshot = await conn.hello()
                self.assertTrue(reply["ok"])
                self.assertEqual(reply["result"]["protocol"], PROTOCOL)
                self.assertEqual(snapshot["event"], "snapshot")
                self.assertIn("player", snapshot["data"])
                self.assertIn("queue", snapshot["data"])
                conn.close()
        run(scenario())

    def test_command_before_hello_is_refused(self):
        async def scenario():
            async with EngineHarness() as h:
                conn = await h.connect()
                await conn.send(id=7, cmd="ping")
                reply = await conn.recv()
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"]["code"], protocol.BAD_REQUEST)
                conn.close()
        run(scenario())

    def test_protocol_mismatch(self):
        async def scenario():
            async with EngineHarness() as h:
                conn = await h.connect()
                reply, _ = await conn.hello(protocol=99)
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"]["code"], protocol.CONFLICT)
                conn.close()
        run(scenario())

    def test_coalesced_lines_and_unknown_command(self):
        async def scenario():
            async with EngineHarness() as h:
                conn = await h.connect()
                await conn.hello()
                await conn.send_raw(b'{"id":2,"cmd":"ping"}\n{"id":3,"cmd":"nope"}\n{"id":4,"cmd":"configure","settings":{"skipBack":"20"}}\n')
                replies = {}
                while len(replies) < 3:
                    reply = await conn.recv()
                    if "id" in reply:            # configure also broadcasts a `config` event
                        replies[reply["id"]] = reply
                self.assertTrue(replies[2]["ok"])
                self.assertEqual(replies[3]["error"]["code"], protocol.BAD_REQUEST)
                self.assertTrue(replies[4]["ok"])
                self.assertEqual(replies[4]["result"]["config"]["skipBack"], 20)
                conn.close()
        run(scenario())

    def test_malformed_json_and_missing_argument(self):
        async def scenario():
            async with EngineHarness() as h:
                conn = await h.connect()
                await conn.hello()
                await conn.send_raw(b"{not json\n")
                reply = await conn.recv()
                self.assertFalse(reply["ok"])
                await conn.send(id=5, cmd="configure")
                reply = await conn.recv()
                self.assertEqual(reply["error"]["code"], protocol.BAD_REQUEST)
                self.assertIn("settings", reply["error"]["message"])
                conn.close()
        run(scenario())

    def test_events_reach_every_greeted_client(self):
        async def scenario():
            async with EngineHarness() as h:
                a = await h.connect()
                b = await h.connect()
                await a.hello()
                await b.hello()
                h.engine.notice("info", "hello there")
                for conn in (a, b):
                    event = await conn.recv()
                    self.assertEqual(event["event"], "notice")
                    self.assertEqual(event["data"]["text"], "hello there")
                a.close()
                b.close()
        run(scenario())

    def test_oversized_line_disconnects(self):
        async def scenario():
            async with EngineHarness() as h:
                conn = await h.connect()
                await conn.hello()
                await conn.send_raw(b'{"id":9,"cmd":"ping","pad":"' + b"x" * (protocol.MAX_LINE + 10) + b'"}\n')
                reply = await conn.recv()
                self.assertFalse(reply["ok"])
                self.assertIsNone(await conn.recv())
                conn.close()
        run(scenario())


class ArgTest(unittest.TestCase):
    def test_coercions(self):
        A = protocol.Arg
        self.assertEqual(A(int).coerce("n", "5"), 5)
        self.assertEqual(A(bool).coerce("b", "true"), True)
        self.assertEqual(A("int-list").coerce("ids", 3), [3])
        self.assertEqual(A("int-list").coerce("ids", ["1", 2]), [1, 2])
        with self.assertRaises(protocol.ProtocolError):
            A(int).coerce("n", "x")
        with self.assertRaises(protocol.ProtocolError):
            A(str, choices=["a"]).coerce("c", "b")
        with self.assertRaises(protocol.ProtocolError):
            A(int, minimum=1).coerce("n", 0)


if __name__ == "__main__":
    unittest.main()


class TrustBoundaryTest(unittest.TestCase):
    """Who may connect and how much one client may cost the daemon."""

    def test_foreign_uid_is_refused(self):
        from unittest import mock
        from engine import server

        async def scenario():
            async with EngineHarness() as h:
                with mock.patch.object(server, "_peer_uid", return_value=os.geteuid() + 1):
                    conn = await h.connect()
                    self.assertTrue(await conn.refused())
                    conn.close()
                # The same client is welcome once the credential matches.
                conn = await h.connect()
                reply, _snapshot = await conn.hello()
                self.assertTrue(reply["ok"])
                conn.close()
        run(scenario())

    def test_connection_cap(self):
        from unittest import mock
        from engine import server

        async def scenario():
            async with EngineHarness() as h:
                with mock.patch.object(server, "MAX_CLIENTS", 2):
                    a = await h.connect()
                    b = await h.connect()
                    await a.hello()
                    await b.hello()
                    c = await h.connect()
                    self.assertTrue(await c.refused())
                    a.close()
                    b.close()
                    c.close()
        run(scenario())

    def test_inflight_cap(self):
        from unittest import mock
        from engine import server
        gate = asyncio.Event()

        async def slow(engine, client):
            await gate.wait()
            return {}

        async def scenario():
            protocol.COMMANDS["test-slow"] = protocol.Command("test-slow", slow, {}, "")
            try:
                async with EngineHarness() as h:
                    conn = await h.connect()
                    await conn.hello()
                    with mock.patch.object(server, "MAX_INFLIGHT", 2):
                        await conn.send(id=10, cmd="test-slow")
                        await conn.send(id=11, cmd="test-slow")
                        await asyncio.sleep(0.1)
                        await conn.send(id=12, cmd="test-slow")
                        reply = await conn.recv()
                        self.assertEqual(reply["id"], 12)
                        self.assertFalse(reply["ok"])
                        self.assertEqual(reply["error"]["code"], protocol.RATE_LIMITED)
                    gate.set()
                    ids = []
                    for _ in range(2):
                        ids.append((await conn.recv())["id"])
                    self.assertEqual(sorted(ids), [10, 11])
                    conn.close()
            finally:
                protocol.COMMANDS.pop("test-slow", None)
        run(scenario())

    def test_slow_reader_is_dropped(self):
        from unittest import mock
        from engine import server

        async def scenario():
            async with EngineHarness() as h:
                conn = await h.connect()
                await conn.hello()
                with mock.patch.object(server, "WRITE_HIGH_WATER", 1024):
                    for index in range(200):
                        h.engine.notice("info", "x" * 200 + str(index))
                    for _ in range(50):
                        if h.engine.server.client_count == 0:
                            break
                        await asyncio.sleep(0.05)
                self.assertEqual(h.engine.server.client_count, 0)
                conn.close()
        run(scenario())

    def test_opml_export_path_rules(self):
        from unittest import mock
        from engine import opml  # noqa: F401  (registers the commands)
        from engine.library import Library

        def attach(engine):
            engine.library = Library(engine)
            engine.subsystems = [engine.library]

        async def scenario():
            async with EngineHarness(attach) as h:
                with mock.patch.dict(os.environ, {"HOME": h.tmp.name}):
                    conn = await h.connect()
                    await conn.hello()
                    await conn.send(id=2, cmd="opml-export", path="/tmp/omarchy-podcast-test-export.opml")
                    reply = await conn.recv()
                    self.assertFalse(reply["ok"])
                    self.assertEqual(reply["error"]["code"], protocol.BAD_REQUEST)
                    link = os.path.join(h.tmp.name, "link.opml")
                    os.symlink(os.path.join(h.tmp.name, "victim"), link)
                    await conn.send(id=3, cmd="opml-export", path=link)
                    reply = await conn.recv()
                    self.assertFalse(reply["ok"])
                    self.assertFalse(os.path.exists(os.path.join(h.tmp.name, "victim")))
                    inside = os.path.join(h.tmp.name, "subs", "mine.opml")
                    os.makedirs(os.path.dirname(inside))
                    await conn.send(id=4, cmd="opml-export", path=inside)
                    reply = await conn.recv()
                    self.assertTrue(reply["ok"], reply)
                    self.assertEqual(oct(os.stat(inside).st_mode & 0o777), "0o600")
                    # Import refuses anything but a regular file.
                    await conn.send(id=5, cmd="opml-import", path=h.tmp.name)
                    reply = await conn.recv()
                    self.assertFalse(reply["ok"])
                    conn.close()
        run(scenario())
