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
