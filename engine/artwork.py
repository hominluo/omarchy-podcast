"""Cover art on disk, in a shape the shell can always draw.

Feeds link to whatever the host uploaded: 3000×3000 PNGs, WebP, the odd
GIF. Stock Omarchy's Qt has no WebP decoder and a grid of huge PNGs is a
grid that eats memory, so every image is fetched once and normalised with
ffmpeg into a JPEG no wider than 600 px, keyed by the hash of its URL. The
QML side only ever sees a file path.
"""

import asyncio
import hashlib
import os
import subprocess

from . import fsio, http, log
from .store import now

LOG = log.get("artwork")

MAX_EDGE = 600
CONCURRENCY = 2
SWEEP_EPISODES = 40


def key_for(url):
    return hashlib.sha1(str(url).encode("utf-8")).hexdigest()


def _sniff(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:12] in (b"ftypavif", b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "avif"
    return ""


class ArtworkCache:
    def __init__(self, engine):
        self.engine = engine
        self.dir = engine.paths.artwork_dir
        self._inflight = {}
        self._semaphore = None
        self._sweep_task = None

    async def start(self):
        self._semaphore = asyncio.Semaphore(CONCURRENCY)
        self.engine.on_settings_changed(lambda: None)

    async def stop(self, restart=False, quit_mpv=True):
        if self._sweep_task and not self._sweep_task.done():
            self._sweep_task.cancel()

    # ---- blocking core (thread) --------------------------------------------

    def path_for(self, url):
        return os.path.join(self.dir, key_for(url) + ".jpg")

    def fetch_and_convert(self, url):
        """Blocking. Returns the cached JPEG path, or "" when the image could
        not be fetched or converted."""
        target = self.path_for(url)
        if os.path.exists(target) and os.path.getsize(target) > 0:
            return target
        try:
            response = http.fetch(url, cap=http.ARTWORK_CAP, timeout=20, accept="image/*")
        except http.FetchError as error:
            LOG.info("artwork %s: %s", url[:80], error.message)
            return ""
        data = response.body
        kind = _sniff(data)
        if not kind:
            return ""
        # Both temps get unpredictable names we created ourselves; ffmpeg's -y
        # then overwrites the empty output file we hand it.
        fd_in, tmp_in = fsio.open_new(self.dir, ".art-", ".src")
        tmp_out = ""
        try:
            with os.fdopen(fd_in, "wb") as handle:
                handle.write(data)
            fd_out, tmp_out = fsio.open_new(self.dir, ".art-", ".jpg")
            os.close(fd_out)
            argv = [
                "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", tmp_in, "-frames:v", "1",
                "-vf", "scale='min(%d,iw)':-2" % MAX_EDGE, "-q:v", "3", "-f", "image2", tmp_out,
            ]
            try:
                result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60, check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                LOG.warning("ffmpeg unavailable for artwork: %s", error)
                result = None
            if result is not None and result.returncode == 0 and os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 0:
                os.replace(tmp_out, target)
                return target
            if kind in ("jpeg", "png"):
                # Qt decodes these itself; keep the original under the .jpg
                # name (QML sniffs content, not extension).
                os.replace(tmp_in, target)
                return target
            return ""
        finally:
            for path in (tmp_in, tmp_out):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    # ---- async API ---------------------------------------------------------

    async def ensure(self, url):
        """Cached path for `url`, fetching if needed; concurrent callers for
        the same URL share one fetch."""
        url = str(url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return ""
        cached = self.path_for(url)
        if os.path.exists(cached) and os.path.getsize(cached) > 0:
            return cached
        future = self._inflight.get(url)
        if future is None:
            future = self.engine.loop.create_future()
            self._inflight[url] = future
            asyncio.ensure_future(self._fetch(url, future))
        return await future

    async def _fetch(self, url, future):
        try:
            async with self._semaphore:
                path = await self.engine.run_in_thread(self.fetch_and_convert, url)
        except Exception as error:  # noqa: BLE001
            LOG.exception("artwork fetch failed")
            path = ""
        finally:
            self._inflight.pop(url, None)
        if not future.done():
            future.set_result(path)

    async def ensure_podcast(self, podcast_id):
        row = self.engine.store.one("SELECT image_url, artwork_path FROM podcasts WHERE id = ?", (int(podcast_id),))
        if row is None or not row["image_url"]:
            return ""
        if row["artwork_path"] and os.path.exists(row["artwork_path"]):
            return row["artwork_path"]
        path = await self.ensure(row["image_url"])
        if path:
            self.engine.store.execute("UPDATE podcasts SET artwork_path = ? WHERE id = ?", (path, int(podcast_id)))
            self.engine.library.broadcast_library()
            playback = getattr(self.engine, "playback", None)
            if playback is not None and playback.current_row is not None and playback.current_row["podcast_id"] == int(podcast_id):
                playback.broadcast_now_playing()
        return path

    async def ensure_episode(self, episode_id):
        row = self.engine.store.one("SELECT image_url, artwork_path FROM episodes WHERE id = ?", (int(episode_id),))
        if row is None or not row["image_url"]:
            return ""
        if row["artwork_path"] and os.path.exists(row["artwork_path"]):
            return row["artwork_path"]
        path = await self.ensure(row["image_url"])
        if path:
            self.engine.store.execute("UPDATE episodes SET artwork_path = ? WHERE id = ?", (path, int(episode_id)))
            self.engine.library.emit_episode(int(episode_id))
            playback = getattr(self.engine, "playback", None)
            if playback is not None and playback.current_id == int(episode_id):
                playback.broadcast_now_playing()
        return path

    def sweep(self):
        """Fetch what is missing for podcasts and for the episodes people look
        at first (inbox, queue, now playing). Runs from the scheduler."""
        if self._sweep_task and not self._sweep_task.done():
            return
        self._sweep_task = asyncio.ensure_future(self._sweep())

    async def _sweep(self):
        store = self.engine.store
        for row in store.all("SELECT id FROM podcasts WHERE image_url != '' AND artwork_path = '' ORDER BY subscribed_at DESC LIMIT 50"):
            await self.ensure_podcast(row["id"])
        rows = store.all(
            """SELECT e.id FROM episodes e
               LEFT JOIN queue q ON q.episode_id = e.id
               WHERE e.image_url != '' AND e.artwork_path = ''
                 AND (q.episode_id IS NOT NULL OR (e.state = 'inbox' AND e.played = 0))
               ORDER BY e.pub_date DESC LIMIT ?""", (SWEEP_EPISODES,))
        for row in rows:
            await self.ensure_episode(row["id"])
        # Episodes with the same picture as their podcast do not need a copy.
        store.execute("UPDATE episodes SET artwork_path = (SELECT artwork_path FROM podcasts p WHERE p.id = episodes.podcast_id) "
                      "WHERE artwork_path = '' AND image_url != '' AND image_url = (SELECT image_url FROM podcasts p WHERE p.id = episodes.podcast_id)")

    def prune(self, keep_days=180):
        """Drop cached files nothing references and that are older than keep_days."""
        referenced = set()
        for row in self.engine.store.all("SELECT artwork_path FROM podcasts WHERE artwork_path != ''"):
            referenced.add(row["artwork_path"])
        for row in self.engine.store.all("SELECT artwork_path FROM episodes WHERE artwork_path != ''"):
            referenced.add(row["artwork_path"])
        cutoff = now() - keep_days * 86400
        removed = 0
        try:
            names = os.listdir(self.dir)
        except OSError:
            return 0
        for name in names:
            path = os.path.join(self.dir, name)
            if path in referenced:
                continue
            try:
                if os.path.getmtime(path) < cutoff:
                    os.unlink(path)
                    removed += 1
            except OSError:
                pass
        return removed
