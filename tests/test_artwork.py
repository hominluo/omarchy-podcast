import asyncio
import os
import shutil
import tempfile
import unittest
from unittest import mock

from engine import artwork, models, protocol
from engine.artwork import ArtworkCache
from engine.library import Library
from engine.store import now
from tests.fakes import EngineHarness, FakeHttpServer



def _png(width=4, height=4):
    """A valid RGB PNG of solid grey, built by hand so no fixture is needed."""
    import struct
    import zlib

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG = _png()


def attach(engine):
    engine.library = Library(engine)
    engine.artwork = ArtworkCache(engine)
    engine.housekeeping_hooks = []
    engine.subsystems = [engine.library, engine.artwork]


def run(coro):
    return asyncio.run(coro)


class ArtworkPathTest(unittest.TestCase):
    """A path is only worth handing to the shell if the file is still there."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.present = os.path.join(self.tmp.name, "cover.jpg")
        with open(self.present, "wb") as handle:
            handle.write(b"jpeg")
        self.gone = os.path.join(self.tmp.name, "gone.jpg")

    def test_missing_file_reads_as_no_artwork(self):
        self.assertEqual(models.artwork_for({"artwork_path": self.present}), self.present)
        # The podcast's cover stands in when the episode's own file has gone.
        self.assertEqual(models.artwork_for({"artwork_path": self.gone,
                                             "podcast_artwork_path": self.present}), self.present)
        self.assertEqual(models.artwork_for({"artwork_path": self.gone,
                                             "podcast_artwork_path": self.gone}), "")

    def test_sweep_forgets_paths_whose_file_vanished(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                store = h.engine.store
                store.execute("INSERT INTO podcasts (feed_url, title, subscribed_at) VALUES (?, ?, ?)",
                              ("http://example.test/feed", "Example", now()))
                podcast_id = store.one("SELECT id FROM podcasts")["id"]
                store.execute("INSERT INTO episodes (podcast_id, guid, title, enclosure_url, first_seen_at) VALUES (?, ?, ?, ?, ?)",
                              (podcast_id, "g1", "Episode", "http://example.test/1.mp3", now()))
                episode_id = store.one("SELECT id FROM episodes")["id"]
                store.execute("UPDATE podcasts SET artwork_path = ? WHERE id = ?", (self.gone, podcast_id))
                store.execute("UPDATE episodes SET artwork_path = ? WHERE id = ?", (self.gone, episode_id))
                h.engine.artwork._forget_missing()
                self.assertEqual(store.one("SELECT artwork_path FROM podcasts")["artwork_path"], "")
                self.assertEqual(store.one("SELECT artwork_path FROM episodes")["artwork_path"], "")
                # A path that still resolves is left alone.
                store.execute("UPDATE podcasts SET artwork_path = ? WHERE id = ?", (self.present, podcast_id))
                h.engine.artwork._forget_missing()
                self.assertEqual(store.one("SELECT artwork_path FROM podcasts")["artwork_path"], self.present)
        run(scenario())


class ArtworkPipelineTest(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttpServer()

    def tearDown(self):
        self.http.close()

    def test_thumbnail_pipeline(self):
        png_url = self.http.add("/cover.png", PNG, "image/png")
        html_url = self.http.add("/page", b"<html>not an image</html>", "text/html")
        lying_url = self.http.add("/lying.png", b"<html>still not</html>", "image/png")

        async def scenario():
            async with EngineHarness(attach) as h:
                art = h.engine.artwork
                if shutil.which("ffmpeg"):
                    path = await art.thumbnail(png_url)
                    self.assertTrue(path.startswith(h.paths.thumbs_dir), path)
                    self.assertTrue(path.endswith(".jpg"))
                    with open(path, "rb") as handle:
                        self.assertEqual(handle.read(3), b"\xff\xd8\xff")   # ffmpeg's JPEG, not the PNG
                    # A second request is served from the cache: no new fetch.
                    before = len(self.http.requests)
                    self.assertEqual(await art.thumbnail(png_url), path)
                    self.assertEqual(len(self.http.requests), before)
                self.assertEqual(await art.thumbnail(html_url), "")
                self.assertEqual(await art.thumbnail(lying_url), "")
                self.assertEqual(await art.thumbnail("javascript:alert(1)"), "")
                self.assertEqual(await art.thumbnail("file:///etc/passwd"), "")
                self.assertEqual(await art.thumbnail("https://x/" + "a" * 3000), "")
                # Nothing but ffmpeg output is ever installed: with ffmpeg
                # failing, the raw bytes do not land in the cache.
                with mock.patch.object(artwork, "_convert", return_value=False):
                    self.assertEqual(await art.thumbnail(self.http.add("/other.png", PNG, "image/png")), "")
                self.assertEqual([name for name in os.listdir(h.paths.thumbs_dir) if not name.startswith(".")],
                                 [os.path.basename(path)] if shutil.which("ffmpeg") else [])
                # The IPC command answers None for anything it cannot produce.
                cmd = protocol.lookup("artwork-thumb")
                self.assertEqual(await cmd.handler(h.engine, None, url="javascript:alert(1)"), {"path": None})
        run(scenario())

    def test_trim_keeps_cache_bounded(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                art = h.engine.artwork
                for index in range(5):
                    with open(os.path.join(h.paths.thumbs_dir, "%d.jpg" % index), "wb") as handle:
                        handle.write(b"x" * 1000)
                    os.utime(os.path.join(h.paths.thumbs_dir, "%d.jpg" % index), (index, index))
                with mock.patch.object(artwork, "THUMB_CACHE_BYTES", 2500):
                    art._trim_thumbs()
                self.assertEqual(sorted(os.listdir(h.paths.thumbs_dir)), ["3.jpg", "4.jpg"])
        run(scenario())

    def test_fetch_image_checks_type_and_magic(self):
        self.assertEqual(artwork._fetch_image(self.http.add("/a.png", PNG, "image/png")), PNG)
        self.assertEqual(artwork._fetch_image(self.http.add("/b.png", PNG, "application/octet-stream")), PNG)
        self.assertIsNone(artwork._fetch_image(self.http.add("/c.png", PNG, "text/html")))
        self.assertIsNone(artwork._fetch_image(self.http.add("/d.png", b"GIF89a" + b"\x00" * 10, "text/plain")))
        self.assertIsNone(artwork._fetch_image(self.http.add("/e.png", b"plain", "image/png")))
