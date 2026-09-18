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
        # Only audio extensions ever reach the library; anything else is "mp3".
        self.assertEqual(extension_for("", "https://x/y/run.sh"), "mp3")
        self.assertEqual(extension_for("", "https://x/y/list.M3U"), "mp3")
        self.assertEqual(extension_for("", "https://x/y/book.m4b"), "m4b")


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
                waiter = asyncio.ensure_future(h.engine.downloads.wait_for(ep["id"]))
                # The first attempt fails and a retry is scheduled; waiters
                # stay pending across retries, so poll the row instead.
                for _ in range(400):
                    row = h.engine.store.one("SELECT status, attempts, temp_path FROM downloads WHERE episode_id = ?", (ep["id"],))
                    if row["attempts"] == 1 and row["status"] == "queued":
                        break
                    await asyncio.sleep(0.05)
                self.assertEqual(row["status"], "queued")
                self.assertEqual(row["attempts"], 1)
                self.assertFalse(waiter.done())
                self.assertTrue(os.path.exists(row["temp_path"]))
                partial = os.path.getsize(row["temp_path"])
                self.assertGreater(partial, 0)
                # Let it resume right away rather than after the retry delay.
                self.http.fail_after_bytes = None
                h.engine.downloads._enqueue(ep["id"], 1, 0)
                path = await asyncio.wait_for(waiter, 20)
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

    def test_unclean_shutdown_resumes_downloading_rows(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                h.engine.settings.update({"downloadDir": os.path.join(h.tmp.name, "music")})
                podcast = await h.engine.library.subscribe(self.feed_url)
                ep = h.engine.library.episodes_page(podcast["id"])["items"][0]
                # A crash mid-transfer leaves the row 'downloading' with no job.
                h.engine.store.execute(
                    "INSERT INTO downloads (episode_id, status, keep, path, temp_path, bytes_done, bytes_total, attempts, error, created_at) "
                    "VALUES (?, 'downloading', 1, '', '', 0, 0, 0, '', 1)", (ep["id"],))
                downloads = h.engine.downloads
                await downloads.stop()
                downloads.jobs.clear()
                downloads._workers.clear()
                h.engine.store.execute("UPDATE downloads SET status = 'downloading' WHERE episode_id = ?", (ep["id"],))
                await downloads.start()
                path = await asyncio.wait_for(downloads.wait_for(ep["id"]), 20)
                self.assertTrue(path and os.path.exists(path))
                self.assertEqual(h.engine.store.scalar("SELECT status FROM downloads WHERE episode_id = ?", (ep["id"],)), "done")
        run(scenario())


if __name__ == "__main__":
    unittest.main()


class DownloadBoundsTest(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttpServer()
        self.audio = bytes(range(256)) * 4000  # 1 MB
        body = fixture("podcasting20.xml").replace(b"https://cdn.example.com/ep2.mp3", self.http.url("/ep2.mp3").encode())
        self.feed_url = self.http.add("/feed.xml", body, "application/rss+xml")

    def tearDown(self):
        self.http.close()

    async def _failed_row(self, h, ep_id):
        for _ in range(400):
            row = h.engine.store.one("SELECT status, error, temp_path FROM downloads WHERE episode_id = ?", (ep_id,))
            if row is not None and row["status"] in ("queued", "failed") and row["error"]:
                return row
            await asyncio.sleep(0.05)
        self.fail("download never failed")

    def test_declared_size_over_cap_is_refused(self):
        from unittest import mock
        from engine import downloads
        self.http.add("/ep2.mp3", self.audio, "audio/mpeg")

        async def scenario():
            async with EngineHarness(attach) as h:
                h.engine.settings.update({"downloadDir": os.path.join(h.tmp.name, "music")})
                podcast = await h.engine.library.subscribe(self.feed_url)
                ep = h.engine.library.episodes_page(podcast["id"])["items"][0]
                with mock.patch.object(downloads, "ABSOLUTE_CAP", 500 * 1024):
                    h.engine.downloads.request([ep["id"]])
                    row = await self._failed_row(h, ep["id"])
                self.assertIn("larger than", row["error"])
                self.assertFalse(row["temp_path"] and os.path.exists(row["temp_path"]) and os.path.getsize(row["temp_path"]))
                self.assertEqual([r for r in self.http.requests if r[1] == "/ep2.mp3"][0][0], "GET")
        run(scenario())

    def test_body_without_length_is_capped(self):
        from unittest import mock
        from engine import downloads
        self.http.add("/ep2.mp3", self.audio, "audio/mpeg", content_length=False)

        async def scenario():
            async with EngineHarness(attach) as h:
                h.engine.settings.update({"downloadDir": os.path.join(h.tmp.name, "music")})
                podcast = await h.engine.library.subscribe(self.feed_url)
                ep = h.engine.library.episodes_page(podcast["id"])["items"][0]
                with mock.patch.object(downloads, "ABSOLUTE_CAP", 300 * 1024), mock.patch.object(downloads, "SIZE_SLACK", 1):
                    h.engine.downloads.request([ep["id"]])
                    row = await self._failed_row(h, ep["id"])
                self.assertIn("larger", row["error"])
                if row["temp_path"] and os.path.exists(row["temp_path"]):
                    self.assertLessEqual(os.path.getsize(row["temp_path"]), 300 * 1024 + downloads.CHUNK)
        run(scenario())

    def test_cover_and_promote_never_follow_links(self):
        self.http.add("/ep2.mp3", self.audio, "audio/mpeg")

        async def scenario():
            async with EngineHarness(attach) as h:
                music = os.path.join(h.tmp.name, "music")
                h.engine.settings.update({"downloadDir": music})
                podcast = await h.engine.library.subscribe(self.feed_url)
                ep = h.engine.library.episodes_page(podcast["id"])["items"][0]
                # Give the podcast some artwork so a cover would be written.
                art = os.path.join(h.tmp.name, "art.jpg")
                with open(art, "wb") as handle:
                    handle.write(b"\xff\xd8\xff" + b"a" * 100)
                h.engine.store.execute("UPDATE podcasts SET artwork_path = ? WHERE id = ?", (art, podcast["id"]))
                # Plant a symlink where cover.jpg would go.
                victim = os.path.join(h.tmp.name, "victim")
                with open(victim, "wb") as handle:
                    handle.write(b"keep me")
                folder = os.path.join(music, "Example Show")
                os.makedirs(folder, exist_ok=True)
                os.symlink(victim, os.path.join(folder, "cover.jpg"))
                h.engine.downloads.request([ep["id"]], keep=1)
                path = await asyncio.wait_for(h.engine.downloads.wait_for(ep["id"]), 20)
                self.assertTrue(path and os.path.isfile(path))
                self.assertEqual(os.path.dirname(path), folder)
                with open(victim, "rb") as handle:
                    self.assertEqual(handle.read(), b"keep me")
                self.assertTrue(os.path.islink(os.path.join(folder, "cover.jpg")))
        run(scenario())
