import asyncio
import os
import unittest

from engine import protocol
from engine.library import Library
from engine.queue import Queue
from tests.fakes import EngineHarness, FakeHttpServer

HERE = os.path.dirname(os.path.abspath(__file__))


def fixture(name):
    with open(os.path.join(HERE, "fixtures", "feeds", name), "rb") as handle:
        return handle.read()


def attach(engine):
    engine.library = Library(engine)
    engine.queue = Queue(engine)
    engine.subsystems = [engine.library, engine.queue]


def run(coro):
    return asyncio.run(coro)


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttpServer()
        self.feed_url = self.http.add("/feed.xml", fixture("podcasting20.xml"), "application/rss+xml", etag='"v1"')

    def tearDown(self):
        self.http.close()

    def test_subscribe_inbox_and_refresh(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                lib = h.engine.library
                podcast = await lib.subscribe(self.feed_url)
                self.assertEqual(podcast["title"], "Example Show")
                self.assertEqual(podcast["counts"]["total"], 3)
                # Only the newest episode lands in the inbox on first fetch.
                inbox = lib.inbox_page()
                self.assertEqual(inbox["count"], 1)
                self.assertEqual(inbox["items"][0]["title"], "Episode 2: Tags")
                self.assertEqual(inbox["items"][0]["artwork"], "https://example.com/ep2.jpg")
                self.assertTrue(inbox["items"][0]["hasTranscriptSource"])
                # Subscribing again is idempotent.
                again = await lib.subscribe(self.feed_url)
                self.assertEqual(again["id"], podcast["id"])
                # A 304 leaves everything alone.
                result = await lib.refresh_podcast(podcast["id"])
                self.assertTrue(result.get("notModified"))
                # A changed feed adds the new item to the inbox.
                new_item = '<item><title>Episode 3</title><guid>ep-3</guid><pubDate>Thu, 03 Sep 2026 10:00:00 +0000</pubDate><enclosure url="https://cdn.example.com/ep3.mp3" type="audio/mpeg"/></item>'
                body = fixture("podcasting20.xml").replace(b"<item>", new_item.encode() + b"<item>", 1)
                self.http.add("/feed.xml", body, "application/rss+xml", etag='"v2"')
                result = await lib.refresh_podcast(podcast["id"])
                self.assertEqual(len(result["added"]), 1)
                self.assertEqual(lib.inbox_page()["count"], 2)
                changed = h.events_named("episodes-changed")
                self.assertEqual(changed[-1]["podcastId"], podcast["id"])
                # Episode paging and detail.
                page = lib.episodes_page(podcast["id"], 0, 2)
                self.assertEqual(page["total"], 4)
                self.assertEqual(len(page["items"]), 2)
                self.assertEqual(page["items"][0]["title"], "Episode 3")
                detail = h.engine.library.require_episode(page["items"][1]["id"])
                self.assertEqual(detail["title"], "Episode 2: Tags")
        run(scenario())

    def test_failed_feed_backs_off(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                lib = h.engine.library
                podcast = await lib.subscribe(self.feed_url)
                self.http.add("/feed.xml", b"", status=500)
                result = await lib.refresh_podcast(podcast["id"])
                self.assertIn("error", result)
                row = h.engine.store.one("SELECT fail_count, last_fetch_ok, next_refresh_at FROM podcasts WHERE id = ?", (podcast["id"],))
                self.assertEqual(row["fail_count"], 1)
                self.assertEqual(row["last_fetch_ok"], 0)
                with self.assertRaises(protocol.ProtocolError):
                    await lib.subscribe(self.http.url("/missing.xml"))
        run(scenario())

    def test_queue_and_played(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                lib, queue = h.engine.library, h.engine.queue
                podcast = await lib.subscribe(self.feed_url)
                ids = [e["id"] for e in lib.episodes_page(podcast["id"])["items"]]
                queue.add([ids[0], ids[1]], "last")
                queue.add([ids[2]], "next")
                self.assertEqual(queue.ids(), [ids[2], ids[0], ids[1]])
                queue.move(ids[1], 0)
                self.assertEqual(queue.ids(), [ids[1], ids[2], ids[0]])
                self.assertEqual(queue.head(), ids[1])
                queue.remove([ids[2]])
                self.assertEqual(queue.ids(), [ids[1], ids[0]])
                self.assertTrue(lib.require_episode(ids[1])["queue_position"] is not None)
                # Marking played drops the episode from the queue and archives it.
                lib.mark_played([ids[1]], True)
                self.assertEqual(queue.ids(), [ids[0]])
                row = lib.require_episode(ids[1])
                self.assertEqual(row["played"], 1)
                self.assertEqual(row["state"], "archived")
                actions = h.engine.store.all("SELECT action FROM episode_actions")
                self.assertIn("play", [a["action"] for a in actions])
                lib.mark_played([ids[1]], False)
                self.assertEqual(lib.require_episode(ids[1])["played"], 0)
                self.assertEqual(queue.pop_head(), ids[0])
                self.assertIsNone(queue.head())
                self.assertEqual(len(h.events_named("queue")) >= 5, True)
        run(scenario())

    def test_unsubscribe_cascades(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                lib = h.engine.library
                podcast = await lib.subscribe(self.feed_url)
                lib.unsubscribe(podcast["id"])
                self.assertEqual(h.engine.store.scalar("SELECT COUNT(*) FROM episodes"), 0)
                self.assertEqual(h.engine.store.scalar("SELECT COUNT(*) FROM subscription_changes WHERE action = 'remove'"), 1)
        run(scenario())


if __name__ == "__main__":
    unittest.main()
