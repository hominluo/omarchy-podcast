import asyncio
import json
import os
import unittest

from engine import chapters
from engine.chapters import Chapters
from engine.library import Library
from engine.queue import Queue
from engine.store import now
from tests.fakes import EngineHarness, FakeHttpServer

HERE = os.path.dirname(os.path.abspath(__file__))


def fixture(name):
    with open(os.path.join(HERE, "fixtures", "feeds", name), "rb") as handle:
        return handle.read()


def attach(engine):
    engine.library = Library(engine)
    engine.queue = Queue(engine)
    engine.chapters = Chapters(engine)
    engine.subsystems = [engine.library, engine.queue, engine.chapters]


def run(coro):
    return asyncio.run(coro)


class ChaptersTest(unittest.TestCase):
    def test_parse_json_chapters(self):
        doc = json.dumps({"version": "1.2.0", "chapters": [
            {"startTime": 0, "title": "Intro", "img": "https://x/a.jpg"},
            {"startTime": 120.5, "title": "Hidden", "toc": False},
            {"startTime": 60, "title": "Topic", "url": "javascript:x", "endTime": 90},
            {"startTime": "bad"},
        ]}).encode()
        parsed = chapters.parse_json_chapters(doc)
        self.assertEqual([c["title"] for c in parsed], ["Intro", "Topic"])
        self.assertEqual(parsed[1]["endTime"], 90)
        self.assertEqual(parsed[1]["url"], "")
        self.assertEqual(parsed[0]["img"], "https://x/a.jpg")
        self.assertEqual(chapters.parse_json_chapters(b"nope"), [])

    def test_merge_prefers_feed_titles_and_fills_ends(self):
        embedded = [{"startTime": 0, "title": "ID3 intro"}, {"startTime": 61, "title": "ID3 two"}, {"startTime": 300, "title": "ID3 three"}]
        external = [{"startTime": 0, "title": "Intro"}, {"startTime": 60, "title": ""}]
        merged = chapters.merge(embedded, external, duration=400)
        self.assertEqual([c["title"] for c in merged], ["Intro", "ID3 two", "ID3 three"])
        self.assertEqual([c["endTime"] for c in merged], [60, 300, 400])
        self.assertEqual([c["index"] for c in merged], [0, 1, 2])
        alone = chapters.merge([], [{"startTime": 5}], None)
        self.assertEqual(alone[0]["title"], "Chapter 1")
        self.assertIsNone(alone[0]["endTime"])


class ChaptersCacheTest(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttpServer()
        self.feed_url = self.http.add("/feed.xml", fixture("podcasting20.xml"), "application/rss+xml")

    def tearDown(self):
        self.http.close()

    async def _episode(self, h):
        podcast = await h.engine.library.subscribe(self.feed_url)
        return h.engine.library.episodes_page(podcast["id"])["items"][0]

    def test_failed_fetch_is_not_cached(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                episode = await self._episode(h)
                missing = self.http.url("/missing.json")
                h.engine.store.execute("UPDATE episodes SET chapters_url = ? WHERE id = ?", (missing, episode["id"]))
                await h.engine.chapters._fetch(episode["id"], missing)
                self.assertIsNone(h.engine.chapters.cached(episode["id"]))
                self.assertEqual(h.engine.store.scalar(
                    "SELECT COUNT(*) FROM chapters_cache WHERE episode_id = ?", (episode["id"],)), 0)
                # A later success is picked up (no permanent empty cache).
                good = self.http.add("/ep2.json", json.dumps({"chapters": [
                    {"startTime": 0, "title": "Intro"}, {"startTime": 60, "title": "Topic"}]}).encode(),
                    "application/json")
                await h.engine.chapters._fetch(episode["id"], good)
                cached = h.engine.chapters.cached(episode["id"])
                self.assertEqual([c["title"] for c in cached], ["Intro", "Topic"])
        run(scenario())

    def test_legacy_empty_cache_is_retried_after_the_backoff(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                episode = await self._episode(h)
                h.engine.store.execute(
                    "INSERT INTO chapters_cache (episode_id, source, chapters_json, fetched_at) VALUES (?, 'pi-json', '[]', ?)",
                    (episode["id"], now() - chapters.RETRY_SECONDS - 1))
                self.assertIsNone(h.engine.chapters.cached(episode["id"]))
                h.engine.store.execute(
                    "UPDATE chapters_cache SET fetched_at = ? WHERE episode_id = ?", (now(), episode["id"]))
                self.assertEqual(h.engine.chapters.cached(episode["id"]), [])
        run(scenario())


if __name__ == "__main__":
    unittest.main()
