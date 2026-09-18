"""Cover art on disk, in a shape the shell can always draw.

Feeds link to whatever the host uploaded: 3000×3000 PNGs, WebP, the odd
GIF. Stock Omarchy's Qt has no WebP decoder and a grid of huge PNGs is a
grid that eats memory, so every image is fetched once and normalised with
ffmpeg into a JPEG no wider than 600 px, keyed by the hash of its URL. The
QML side only ever sees a file path — never a remote URL, and never bytes
ffmpeg did not produce: the shell process does not decode what a feed or a
search index served.

Search results and previews get the same treatment on demand through
`artwork-thumb`, into a smaller, size-bounded cache of thumbnails.
"""

import asyncio
import hashlib
import os
import stat
import subprocess
import tempfile

from . import fsio, http, log, protocol
from .store import now

LOG = log.get("artwork")

MAX_EDGE = 600
CONCURRENCY = 2
SWEEP_EPISODES = 40
THUMB_MAX_EDGE = 512
THUMB_CONCURRENCY = 3
THUMB_CACHE_BYTES = 100 * 1024 * 1024
THUMB_MAX_AGE_DAYS = 30
MAX_URL = 2048
IMAGE_TYPES = ("", "application/octet-stream", "binary/octet-stream")


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


def _fetch_image(url):
    """The bytes of an image, or None. The server's type must be an image
    (or say nothing), and the bytes must look like one."""
    try:
        response = http.fetch(url, cap=http.ARTWORK_CAP, timeout=20, accept="image/*")
    except http.FetchError as error:
        LOG.info("artwork %s: %s", http.redact_url(url), error.message)
        return None
    content_type = response.content_type
    if not (content_type.startswith("image/") or content_type in IMAGE_TYPES):
        LOG.info("artwork %s: not an image (%s)", http.redact_url(url), content_type)
        return None
    if not _sniff(response.body):
        return None
    return response.body


def _convert(data, directory, target, max_edge):
    """ffmpeg re-encodes `data` into a JPEG at `target`, no edge longer than
    `max_edge`. False when ffmpeg is missing or refuses the input; nothing
    the server sent is ever installed as is."""
    fd_in, tmp_in = fsio.open_new(directory, ".art-", ".src")
    tmp_out = ""
    try:
        with os.fdopen(fd_in, "wb") as handle:
            handle.write(data)
        # Both temps are unpredictable names we created; ffmpeg's -y then
        # overwrites the empty output we hand it.
        fd_out, tmp_out = fsio.open_new(directory, ".art-", ".jpg")
        os.close(fd_out)
        argv = [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", tmp_in, "-frames:v", "1",
            "-vf", "scale='min(%d,iw)':'min(%d,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2" % (max_edge, max_edge),
            "-q:v", "3", "-f", "image2", tmp_out,
        ]
        with tempfile.TemporaryFile(prefix="ffmpeg-art-") as errlog:
            try:
                result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errlog,
                                        timeout=60, check=False, start_new_session=True)
            except (OSError, subprocess.TimeoutExpired) as error:
                LOG.warning("ffmpeg unavailable for artwork: %s", error)
                return False
            if result.returncode != 0:
                errlog.seek(0)
                tail = errlog.read()[-2048:].decode("utf-8", "replace").strip().splitlines()
                LOG.info("ffmpeg refused an image: %s", tail[-1][:200] if tail else "exit %d" % result.returncode)
                return False
        if os.path.getsize(tmp_out) <= 0:
            return False
        os.replace(tmp_out, target)
        tmp_out = ""
        return True
    finally:
        for path in (tmp_in, tmp_out):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


class ArtworkCache:
    def __init__(self, engine):
        self.engine = engine
        self.dir = engine.paths.artwork_dir
        self.thumbs_dir = engine.paths.thumbs_dir
        self._inflight = {}
        self._semaphore = None
        self._thumb_inflight = {}
        self._thumb_semaphore = None
        self._sweep_task = None

    async def start(self):
        self._semaphore = asyncio.Semaphore(CONCURRENCY)
        self._thumb_semaphore = asyncio.Semaphore(THUMB_CONCURRENCY)
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
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            return target
        data = _fetch_image(url)
        if data is None or not _convert(data, self.dir, target, MAX_EDGE):
            return ""
        return target

    def thumb_path_for(self, url):
        return os.path.join(self.thumbs_dir, hashlib.sha256(str(url).encode("utf-8")).hexdigest() + ".jpg")

    def fetch_thumbnail(self, url):
        """Blocking. A small re-encoded copy of a remote image for the
        discover/search views, or "" when it cannot be had."""
        target = self.thumb_path_for(url)
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            try:
                os.utime(target)          # most recently used, for the trim
            except OSError:
                pass
            return target
        data = _fetch_image(url)
        if data is None or not _convert(data, self.thumbs_dir, target, THUMB_MAX_EDGE):
            return ""
        self._trim_thumbs()
        return target

    def _trim_thumbs(self):
        """Keep the thumbnail cache under THUMB_CACHE_BYTES, oldest first."""
        entries = []
        try:
            names = os.listdir(self.thumbs_dir)
        except OSError:
            return
        for name in names:
            path = os.path.join(self.thumbs_dir, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                entries.append((info.st_mtime, info.st_size, path))
        total = sum(size for _mtime, size, _path in entries)
        for _mtime, size, path in sorted(entries):
            if total <= THUMB_CACHE_BYTES:
                break
            try:
                os.unlink(path)
                total -= size
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

    async def thumbnail(self, url):
        """A local thumbnail for a remote image URL, fetched once per URL
        with a small concurrency budget; "" when it cannot be produced."""
        url = str(url or "").strip()
        if len(url) > MAX_URL:
            return ""
        try:
            url = http.check_url(url)
        except http.FetchError:
            return ""
        cached = self.thumb_path_for(url)
        if os.path.isfile(cached) and os.path.getsize(cached) > 0:
            return cached
        future = self._thumb_inflight.get(url)
        if future is None:
            future = self.engine.loop.create_future()
            self._thumb_inflight[url] = future
            asyncio.ensure_future(self._fetch_thumb(url, future))
        return await future

    async def _fetch_thumb(self, url, future):
        try:
            async with self._thumb_semaphore:
                path = await self.engine.run_in_thread(self.fetch_thumbnail, url)
        except Exception:  # noqa: BLE001
            LOG.exception("thumbnail fetch failed")
            path = ""
        finally:
            self._thumb_inflight.pop(url, None)
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
        """Drop cached files nothing references and that are older than
        keep_days, and thumbnails older than THUMB_MAX_AGE_DAYS."""
        thumb_cutoff = now() - THUMB_MAX_AGE_DAYS * 86400
        try:
            for name in os.listdir(self.thumbs_dir):
                path = os.path.join(self.thumbs_dir, name)
                try:
                    if os.path.getmtime(path) < thumb_cutoff:
                        os.unlink(path)
                except OSError:
                    pass
        except OSError:
            pass
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


# ---------------------------------------------------------------- commands

@protocol.command("artwork-thumb", "A local, re-encoded thumbnail for a remote image URL", url=protocol.Arg(str))
async def cmd_artwork_thumb(engine, client, url):
    path = await engine.artwork.thumbnail(url)
    return {"path": path or None}
