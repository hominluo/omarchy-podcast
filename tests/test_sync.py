import asyncio
import os
import unittest

from engine.library import Library
from engine.queue import Queue
from engine.scheduler import Scheduler
from engine.sync import Sync, _from_iso, _iso
from tests.fakes import EngineHarness, FakeHttpServer

HERE = os.path.dirname(os.path.abspath(__file__))


def fixture(name):
    with open(os.path.join(HERE, "fixtures", "feeds", name), "rb") as handle:
        return handle.read()


def attach(engine):
    engine.library = Library(engine)
    engine.queue = Queue(engine)
    engine.scheduler = Scheduler(engine)
    engine.sync = Sync(engine)
    engine.subsystems = [engine.library, engine.queue, engine.sync]


def run(coro):
    return asyncio.run(coro)


class FakeGpodder:
    """Enough of gpodder.net API 2 to exercise a cycle."""

    def __init__(self, http):
        self.http = http
        self.subscriptions = []
        self.actions = []
        self.remote_add = []
        self.remote_remove = []
        self.remote_actions = []
        self.devices = []
        user = "alice"
        http.json_routes["/api/2/devices/%s/dev1.json" % user] = self.device
        http.json_routes["/api/2/subscriptions/%s/dev1.json" % user] = self.subs
        http.json_routes["/api/2/episodes/%s.json" % user] = self.episodes

    def _auth(self, params):
        return True

    def device(self, method, params, body):
        self.devices.append(body)
        return {}

    def subs(self, method, params, body):
        if method == "POST":
            self.subscriptions.append(body)
            return {"timestamp": 1000, "update_urls": []}
        return {"add": self.remote_add, "remove": self.remote_remove, "timestamp": 1001}

    def episodes(self, method, params, body):
        if method == "POST":
            self.actions.extend(body)
            return {"timestamp": 1002, "update_urls": []}
        return {"actions": self.remote_actions, "timestamp": 1003}


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttpServer()
        self.feed_url = self.http.add("/feed.xml", fixture("podcasting20.xml"), "application/rss+xml")
        self.remote = FakeGpodder(self.http)

    def tearDown(self):
        self.http.close()

    def _configure(self, h):
        h.engine.apply_settings({"syncProvider": "gpodder", "syncServer": self.http.base, "syncUsername": "alice", "syncDeviceId": "dev1"})
        h.engine.credentials = {"sync": {"password": "pw"}}

    def test_cycle_pushes_and_pulls(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                self._configure(h)
                self.assertTrue(h.engine.sync.enabled)
                podcast = await h.engine.library.subscribe(self.feed_url)
                ids = [e["id"] for e in h.engine.library.episodes_page(podcast["id"])["items"]]
                h.engine.library.mark_played([ids[0]], True)
                # Another client played episode 2 half way and subscribed to a new feed.
                other_feed = self.http.add("/other.xml", fixture("atom.xml"), "application/atom+xml")
                self.remote.remote_add = [other_feed]
                self.remote.remote_actions = [{
                    "podcast": self.feed_url, "episode": "https://cdn.example.com/ep1.mp3", "action": "play",
                    "timestamp": _iso(9999999999), "started": 0, "position": 900, "total": 1830, "device": "phone",
                }]
                result = await h.engine.sync.run()
                self.assertTrue(result["synced"], result)
                # Pushed our subscription and the play action.
                self.assertEqual(self.remote.subscriptions[0]["add"], [self.feed_url])
                pushed = [a for a in self.remote.actions if a["action"] == "play"]
                self.assertTrue(pushed)
                self.assertEqual(pushed[0]["device"], "dev1")
                self.assertEqual(h.engine.store.scalar("SELECT COUNT(*) FROM episode_actions WHERE synced = 0"), 0)
                # Pulled the remote subscription and the remote position.
                titles = [p["title"] for p in h.engine.library.library_list()]
                self.assertIn("Atom Cast", titles)
                row = h.engine.store.one("SELECT position FROM episodes WHERE enclosure_url = ?", ("https://cdn.example.com/ep1.mp3",))
                self.assertEqual(row["position"], 900)
                self.assertEqual(h.engine.state["sync"]["lastError"], "")
                self.assertGreater(h.engine.state["sync"]["lastSyncAt"], 0)
        run(scenario())

    def test_older_remote_position_does_not_win(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                self._configure(h)
                podcast = await h.engine.library.subscribe(self.feed_url)
                ep = h.engine.library.episodes_page(podcast["id"])["items"][1]
                h.engine.library.set_position(ep["id"], 500)
                self.remote.remote_actions = [{
                    "podcast": self.feed_url, "episode": "https://cdn.example.com/ep1.mp3", "action": "play",
                    "timestamp": "2020-01-01T00:00:00", "started": 0, "position": 100, "total": 1830,
                }]
                await h.engine.sync.run()
                row = h.engine.store.one("SELECT position FROM episodes WHERE id = ?", (ep["id"],))
                self.assertEqual(row["position"], 500)
        run(scenario())

    def test_iso_helpers(self):
        self.assertEqual(_from_iso("2020-01-01T00:00:00"), 1577836800)
        self.assertEqual(_from_iso(1577836800), 1577836800)
        self.assertIsNone(_from_iso("bogus"))
        self.assertEqual(_iso(1577836800), "2020-01-01T00:00:00")


if __name__ == "__main__":
    unittest.main()
