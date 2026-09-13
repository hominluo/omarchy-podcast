import asyncio
import os
import unittest

from engine.downloads import Downloads, extension_for, safe_name
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
    engine.downloads = Downloads(engine)
    engine.housekeeping_hooks = []
    engine.subsystems = [engine.library, engine.queue, engine.downloads]


def run(coro):
    return asyncio.run(coro)


class HelpersTest(unittest.TestCase):
    def test_safe_name(self):
        self.assertEqual(safe_name('Ep/1: "Hello" <world>?'), "Ep_1_ _Hello_ _world_")
        self.assertEqual(safe_name("   "), "untitled")
        self.assertLessEqual(len(safe_name("é" * 200, 50).encode()), 50)

    def test_extension(self):
        self.assertEqual(extension_for("audio/mpeg", ""), "mp3")
        self.assertEqual(extension_for("audio/x-m4a", ""), "m4a")
        self.assertEqual(extension_for("", "https://x/y/file.OGG?x=1"), "ogg")
        self.assertEqual(extension_for("application/octet-stream", "https://x/y/noext"), "mp3")


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttpServer()
        self.audio = bytes(range(256)) * 4000  # 1 MB
        body = fixture("podcasting20.xml").replace(b"https://cdn.example.com/ep2.mp3", self.http.url("/ep2.mp3").encode())
        self.feed_url = self.http.add("/feed.xml", body, "application/rss+xml")
        self.http.add("/ep2.mp3", self.audio, "audio/mpeg", etag='"a1"')

    def tearDown(self):
        self.http.close()

    def test_download_and_resume(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                h.engine.settings.update({"downloadDir": os.path.join(h.tmp.name, "music")})
                podcast = await h.engine.library.subscribe(self.feed_url)
                ep = h.engine.library.episodes_page(podcast["id"])["items"][0]
                self.assertEqual(ep["title"], "Episode 2: Tags")
                # First attempt: server drops the connection half way.
                self.http.fail_after_bytes = 300 * 1024
                h.engine.downloads.request([ep["id"]])
                path = await asyncio.wait_for(h.engine.downloads.wait_for(ep["id"]), 20)
                self.assertIsNone(path)  # first attempt failed, retry pending
                row = h.engine.store.one("SELECT status, attempts, temp_path FROM downloads WHERE episode_id = ?", (ep["id"],))
                self.assertEqual(row["status"], "queued")
                self.assertEqual(row["attempts"], 1)
                self.assertTrue(os.path.exists(row["temp_path"]))
                partial = os.path.getsize(row["temp_path"])
                self.assertGreater(partial, 0)
                # Let it resume right away rather than after the retry delay.
                self.http.fail_after_bytes = None
                h.engine.downloads._enqueue(ep["id"], 1, 0)
                path = await asyncio.wait_for(h.engine.downloads.wait_for(ep["id"]), 20)
                self.assertTrue(path and os.path.exists(path))
                with open(path, "rb") as handle:
                    self.assertEqual(handle.read(), self.audio)
                ranged = [r for r in self.http.requests if r[1] == "/ep2.mp3" and "Range" in r[2]]
                self.assertTrue(ranged)
                self.assertEqual(ranged[-1][2]["Range"], "bytes=%d-" % partial)
                self.assertIn("2026-09-02 Episode 2_ Tags.mp3", path)
                summary = h.engine.library.require_episode(ep["id"])
                self.assertEqual(summary["download_status"], "done")
                self.assertEqual(h.engine.state["downloads"][0]["download"], "done")
                # Delete removes the file and the row.
                h.engine.downloads.delete([ep["id"]])
                self.assertFalse(os.path.exists(path))
                self.assertEqual(h.engine.store.scalar("SELECT COUNT(*) FROM downloads"), 0)
        run(scenario())

    def test_cleanup_removes_old_played(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                h.engine.settings.update({"downloadDir": os.path.join(h.tmp.name, "music"), "deletePlayedAfterDays": 1})
                podcast = await h.engine.library.subscribe(self.feed_url)
                ep = h.engine.library.episodes_page(podcast["id"])["items"][0]
                h.engine.downloads.request([ep["id"]])
                path = await asyncio.wait_for(h.engine.downloads.wait_for(ep["id"]), 20)
                self.assertTrue(os.path.exists(path))
                h.engine.store.execute("UPDATE episodes SET played = 1, played_at = ? WHERE id = ?", (1, ep["id"]))
                result = h.engine.downloads.cleanup()
                self.assertEqual(result["removed"], 1)
                self.assertFalse(os.path.exists(path))
        run(scenario())


if __name__ == "__main__":
    unittest.main()
