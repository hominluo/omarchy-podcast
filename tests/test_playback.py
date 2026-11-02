import asyncio
import os
import unittest

from engine import protocol
from engine.library import Library
from engine.playback import Playback
from engine.player import MpvError
from engine.queue import Queue
from tests.fakes import EngineHarness, FakeHttpServer

HERE = os.path.dirname(os.path.abspath(__file__))


def fixture(name):
    with open(os.path.join(HERE, "fixtures", "feeds", name), "rb") as handle:
        return handle.read()


class FakeMpv:
    """Just enough of MpvClient for play(): the command either works or is
    refused before anything is loaded."""

    def __init__(self, fail_load=False):
        self.state = "connected"
        self.props = {}
        self.fail_load = fail_load
        self.commands = []
        self.on_property = None
        self.on_event = None
        self.on_connect = None
        self.on_disconnect = None

    async def command(self, *args, **kwargs):
        self.commands.append(args)
        if args and args[0] == "loadfile" and self.fail_load:
            raise MpvError("refused")
        return None

    async def set_property(self, *args, **kwargs):
        return None

    def command_nowait(self, *args, **kwargs):
        return None


def attach(fail_load=False):
    def wire(engine):
        engine.library = Library(engine)
        engine.queue = Queue(engine)
        engine.mpv = FakeMpv(fail_load=fail_load)
        engine.playback = Playback(engine)
        engine.subsystems = [engine.library, engine.queue, engine.playback]
    return wire


def run(coro):
    return asyncio.run(coro)


class PlayQueueTest(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttpServer()
        self.feed_url = self.http.add("/feed.xml", fixture("podcasting20.xml"), "application/rss+xml")

    def tearDown(self):
        self.http.close()

    def test_refused_loadfile_keeps_the_episode_in_up_next(self):
        async def scenario():
            async with EngineHarness(attach(fail_load=True)) as h:
                podcast = await h.engine.library.subscribe(self.feed_url)
                episode = h.engine.library.episodes_page(podcast["id"])["items"][0]
                h.engine.queue.add([episode["id"]])
                self.assertTrue(h.engine.queue.contains(episode["id"]))
                with self.assertRaises(protocol.ProtocolError):
                    await h.engine.playback.play(episode["id"])
                self.assertTrue(h.engine.queue.contains(episode["id"]))
                self.assertIsNone(h.engine.playback.current_id)
                self.assertIsNone(h.engine.store.get_setting("lastEpisodeId"))
        run(scenario())

    def test_successful_loadfile_removes_the_episode_from_up_next(self):
        async def scenario():
            async with EngineHarness(attach(fail_load=False)) as h:
                podcast = await h.engine.library.subscribe(self.feed_url)
                episode = h.engine.library.episodes_page(podcast["id"])["items"][0]
                h.engine.queue.add([episode["id"]])
                await h.engine.playback.play(episode["id"])
                self.assertFalse(h.engine.queue.contains(episode["id"]))
                self.assertEqual(h.engine.playback.current_id, episode["id"])
                self.assertEqual(h.engine.store.get_setting("lastEpisodeId"), episode["id"])
        run(scenario())


if __name__ == "__main__":
    unittest.main()
