"""Downloads: keep episodes on disk for offline listening and transcription.

Two workers pull from a priority queue (what the user asked for first, then
auto-downloads, then audio fetched only to transcribe). A download writes to
`<name>.part` and resumes with a Range request when interrupted; the final
rename is atomic, so a file either is complete or is not there. Progress
goes out as events at most twice a second per job.

`keep=1` rows are the user's library on disk under the download folder;
`keep=0` rows live in the cache and are evicted once used.
"""

import asyncio
import os
import re
import secrets
import shutil
import threading
import time
import unicodedata

from . import fsio, http, log, models, protocol
from .store import now

LOG = log.get("downloads")
A = protocol.Arg

WORKERS = 2
CHUNK = 256 * 1024
PROGRESS_INTERVAL = 0.5
RETRY_DELAYS = (5, 30, 120)
FREE_SPACE_FACTOR = 2
PRIORITY_USER, PRIORITY_AUTO, PRIORITY_CACHE = 0, 1, 2
CACHE_MAX_AGE = 24 * 3600
PART_MAX_AGE = 7 * 86400
# A transfer may not exceed this many times the size the feed declared (or
# this absolute size when it declared none): a chunked response that never
# ends must not fill the disk.
SIZE_SLACK = 4
ABSOLUTE_CAP = 2 * 1024 ** 3
STALL_SECONDS = 90

EXTENSIONS = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/m4a": "m4a",
    "audio/aac": "aac", "audio/ogg": "ogg", "audio/opus": "opus", "audio/flac": "flac", "audio/x-flac": "flac",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/webm": "webm", "video/mp4": "mp4", "video/x-m4v": "m4v",
}


class Cancelled(Exception):
    pass


def safe_name(value, limit=120):
    text = unicodedata.normalize("NFC", str(value or "")[:limit * 4]).strip()
    text = re.sub(r"[\x00-\x1f/\\<>:\"|?*]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "untitled"
    # Cut on a UTF-8 boundary in one step rather than a character at a time.
    text = text.encode("utf-8")[:limit].decode("utf-8", "ignore")
    return text.rstrip(" .") or "untitled"


def extension_for(mime, url):
    ext = EXTENSIONS.get(str(mime or "").split(";")[0].strip().lower())
    if ext:
        return ext
    path = str(url or "").split("?")[0].split("#")[0]
    tail = path.rsplit(".", 1)
    if len(tail) == 2 and 1 < len(tail[1]) <= 4 and tail[1].isalnum():
        return tail[1].lower()
    return "mp3"


class Job:
    def __init__(self, episode_id, keep, priority, seq):
        self.episode_id = episode_id
        self.keep = keep
        self.priority = priority
        self.seq = seq           # ties the job to its queue entry
        self.cancel = threading.Event()
        self.started = False
        self.bytes_done = 0
        self.bytes_total = None
        self.rate = 0.0


class Downloads:
    def __init__(self, engine):
        self.engine = engine
        self.store = None
        self.queue = None
        self.jobs = {}
        self._seq = 0
        self._workers = []
        self._last_progress = {}
        self._waiters = {}

    # ---- lifecycle ---------------------------------------------------------

    async def start(self):
        self.store = self.engine.store
        self.queue = asyncio.PriorityQueue()
        self.engine.on_new_episodes_extra = self._on_new_episodes
        self.engine.housekeeping_hooks.append(self.cleanup)
        # Whatever was in flight when the daemon last stopped (cleanly or not)
        # goes back in line.
        self.store.execute("UPDATE downloads SET status = 'queued' WHERE status = 'downloading'")
        for row in self.store.all("SELECT episode_id, keep FROM downloads WHERE status = 'queued' ORDER BY created_at"):
            self._enqueue(row["episode_id"], row["keep"], PRIORITY_AUTO if row["keep"] else PRIORITY_CACHE)
        for _ in range(WORKERS):
            self._workers.append(asyncio.ensure_future(self._worker()))
        self.broadcast()

    async def stop(self, restart=False, quit_mpv=True):
        for job in list(self.jobs.values()):
            if job.cancel is not None:
                job.cancel.set()
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.store.execute("UPDATE downloads SET status = 'queued' WHERE status = 'downloading'")

    # ---- queueing ----------------------------------------------------------

    def _enqueue(self, episode_id, keep, priority):
        current = self.jobs.get(episode_id)
        if current is not None:
            # Either waiting in line, or a cancelled transfer whose thread may
            # still hold the .part file: the worker re-enqueues when it drains,
            # at the most urgent priority asked for meanwhile.
            current.priority = min(current.priority, priority)
            return current
        self._seq += 1
        job = Job(int(episode_id), int(keep), priority, self._seq)
        self.jobs[job.episode_id] = job
        self.queue.put_nowait((priority, job.seq, job.episode_id))
        return job

    def request(self, episode_ids, keep=1, priority=PRIORITY_USER):
        added = []
        for episode_id in episode_ids:
            row = self.store.one("SELECT id FROM episodes WHERE id = ?", (int(episode_id),))
            if row is None:
                continue
            existing = self.store.one("SELECT status, keep, path FROM downloads WHERE episode_id = ?", (row["id"],))
            if existing is not None and existing["status"] == "done" and existing["path"] and os.path.exists(existing["path"]):
                if keep and not existing["keep"]:
                    asyncio.ensure_future(self._promote(row["id"], existing["path"]))
                    added.append(row["id"])
                continue
            stamp = now()
            self.store.execute(
                "INSERT INTO downloads (episode_id, status, keep, created_at) VALUES (?, 'queued', ?, ?) "
                "ON CONFLICT(episode_id) DO UPDATE SET status = 'queued', keep = MAX(keep, excluded.keep), error = '', attempts = 0",
                (row["id"], int(bool(keep)), stamp))
            self._enqueue(row["id"], keep, priority)
            added.append(row["id"])
        if added:
            self.broadcast()
            for episode_id in added:
                self.engine.library.emit_episode(episode_id)
        return {"queued": added}

    def wait_for(self, episode_id):
        """Future resolved with the file path once `episode_id` is on disk (or
        None when it failed). Used by the transcriber."""
        future = self.engine.loop.create_future()
        row = self.store.one("SELECT status, path FROM downloads WHERE episode_id = ?", (int(episode_id),))
        if row is not None and row["status"] == "done" and row["path"] and os.path.exists(row["path"]):
            future.set_result(row["path"])
            return future
        self._waiters.setdefault(int(episode_id), []).append(future)
        return future

    def _resolve_waiters(self, episode_id, path):
        for future in self._waiters.pop(episode_id, []):
            if not future.done():
                future.set_result(path)

    def cancel(self, episode_ids, quiet=False):
        cancelled = []
        for episode_id in episode_ids:
            episode_id = int(episode_id)
            job = self.jobs.get(episode_id)
            if job is not None:
                job.cancel.set()
                if not job.started:
                    # Still waiting in line: the worker skips the stale entry.
                    self.jobs.pop(episode_id, None)
            self.store.execute("UPDATE downloads SET status = 'paused' WHERE episode_id = ? AND status IN ('queued', 'downloading')", (episode_id,))
            cancelled.append(episode_id)
            self._resolve_waiters(episode_id, None)
        if quiet:
            return {"cancelled": cancelled}
        self.broadcast()
        for episode_id in cancelled:
            self.engine.library.emit_episode(episode_id)
        return {"cancelled": cancelled}

    def delete(self, episode_ids):
        deleted = []
        for episode_id in episode_ids:
            episode_id = int(episode_id)
            self.cancel([episode_id])
            row = self.store.one("SELECT path, temp_path FROM downloads WHERE episode_id = ?", (episode_id,))
            if row is None:
                continue
            for path in (row["path"], row["temp_path"]):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            self.store.execute("DELETE FROM downloads WHERE episode_id = ?", (episode_id,))
            self.engine.library.record_action(episode_id, "delete")
            deleted.append(episode_id)
        self.broadcast()
        for episode_id in deleted:
            self.engine.library.emit_episode(episode_id)
        playback = getattr(self.engine, "playback", None)
        if playback is not None and playback.current_id in deleted:
            playback.broadcast_now_playing()
        return {"deleted": deleted}

    # ---- state -------------------------------------------------------------

    def items(self):
        rows = self.store.all(
            models.EPISODE_SELECT + " WHERE d.episode_id IS NOT NULL AND d.keep = 1 ORDER BY "
            "CASE d.status WHEN 'downloading' THEN 0 WHEN 'queued' THEN 1 WHEN 'paused' THEN 2 WHEN 'error' THEN 3 ELSE 4 END, d.created_at DESC")
        items = []
        for row in rows:
            summary = models.episode_summary(row)
            job = self.jobs.get(row["id"])
            summary["downloadStatus"] = row["download_status"]
            summary["bytesDone"] = job.bytes_done if job else (row["download_bytes_done"] or 0)
            summary["bytesTotal"] = job.bytes_total if job and job.bytes_total else row["download_bytes_total"]
            summary["downloadRate"] = job.rate if job else 0
            summary["downloadError"] = self.store.scalar("SELECT error FROM downloads WHERE episode_id = ?", (row["id"],), default="")
            items.append(summary)
        return items

    def broadcast(self):
        self.engine.set_state("downloads", self.items())

    def _progress(self, job, force=False):
        stamp = time.monotonic()
        if not force and stamp - self._last_progress.get(job.episode_id, 0) < PROGRESS_INTERVAL:
            return
        self._last_progress[job.episode_id] = stamp
        self.engine.emit("download-progress", {
            "episodeId": job.episode_id, "bytesDone": job.bytes_done, "bytesTotal": job.bytes_total,
            "rate": round(job.rate), "status": "downloading",
        })

    # ---- workers -----------------------------------------------------------

    async def _worker(self):
        while True:
            _, seq, episode_id = await self.queue.get()
            job = self.jobs.get(episode_id)
            if job is None or job.seq != seq or job.cancel.is_set() or job.started:
                continue
            row = self.store.one("SELECT status FROM downloads WHERE episode_id = ?", (episode_id,))
            if row is None or row["status"] != "queued":
                self.jobs.pop(episode_id, None)
                continue
            job.started = True
            self.store.execute("UPDATE downloads SET status = 'downloading' WHERE episode_id = ?", (episode_id,))
            self.broadcast()
            self.engine.library.emit_episode(episode_id)
            path = None
            settled = True
            try:
                plan = self._plan(job)
                path = await self.engine.run_in_thread(self._download, job, plan)
                self.store.execute("UPDATE downloads SET etag = ?, last_modified = ? WHERE episode_id = ?",
                                   (plan.get("etag"), plan.get("last_modified"), episode_id))
                self._cover_for(plan)
            except Cancelled:
                # cancel() already flipped the row to 'paused'; a re-requested
                # ('queued') or deleted row stays untouched.
                self.store.execute("UPDATE downloads SET status = 'paused', bytes_done = ? WHERE episode_id = ? AND status IN ('downloading', 'paused')",
                                   (job.bytes_done, episode_id))
            except http.FetchError as error:
                settled = await self._failed(job, error.message)
            except Exception as error:  # noqa: BLE001
                if not job.cancel.is_set():
                    LOG.exception("download of episode %d crashed", episode_id)
                settled = await self._failed(job, "%s: %s" % (type(error).__name__, error))
            else:
                self.store.execute(
                    "UPDATE downloads SET status = 'done', path = ?, temp_path = '', bytes_done = ?, bytes_total = ?, finished_at = ?, error = '' WHERE episode_id = ?",
                    (path, job.bytes_done, job.bytes_total or job.bytes_done, now(), episode_id))
                if job.keep:
                    self.engine.library.record_action(episode_id, "download")
                LOG.info("downloaded episode %d -> %s", episode_id, path)
                hook = getattr(self.engine, "on_download_done", None)
                if hook:
                    try:
                        hook(episode_id, job.keep)
                    except Exception:  # noqa: BLE001
                        LOG.exception("download hook failed")
            if self.jobs.get(episode_id) is job:
                self.jobs.pop(episode_id, None)
            if job.cancel.is_set():
                # Re-requested while the cancelled transfer drained: its thread
                # has exited now, so the .part file is free again.
                state = self.store.one("SELECT status, keep FROM downloads WHERE episode_id = ?", (episode_id,))
                if state is not None and state["status"] == "queued":
                    self._enqueue(episode_id, state["keep"], job.priority)
                    settled = False
            if settled:
                # A retry keeps the waiters (the transcriber) pending; only a
                # final outcome resolves them.
                self._resolve_waiters(episode_id, path)
            self.broadcast()
            self.engine.library.emit_episode(episode_id)
            playback = getattr(self.engine, "playback", None)
            if playback is not None and playback.current_id == episode_id:
                playback.broadcast_now_playing()

    async def _failed(self, job, message):
        """Returns True when the failure is final, False when a retry is due."""
        if job.cancel.is_set():
            # cancel() or an unsubscribe already settled the row ('paused' or
            # gone); the thread merely noticed late.
            LOG.info("download of episode %d cancelled (%s)", job.episode_id, message)
            return True
        row = self.store.one("SELECT attempts FROM downloads WHERE episode_id = ?", (job.episode_id,))
        attempts = int(row["attempts"] or 0) + 1 if row else 1
        if attempts <= len(RETRY_DELAYS):
            delay = RETRY_DELAYS[attempts - 1]
            LOG.warning("download of episode %d failed (%s); retry in %ds", job.episode_id, message, delay)
            self.store.execute("UPDATE downloads SET attempts = ?, error = ?, status = 'queued' WHERE episode_id = ?", (attempts, message[:200], job.episode_id))
            self.engine.loop.call_later(delay, self._retry, job.episode_id, job.keep, job.priority)
            return False
        LOG.error("download of episode %d gave up: %s", job.episode_id, message)
        self.store.execute("UPDATE downloads SET attempts = ?, error = ?, status = 'error' WHERE episode_id = ?", (attempts, message[:200], job.episode_id))
        title = self.store.scalar("SELECT title FROM episodes WHERE id = ?", (job.episode_id,), default="episode")
        self.engine.notice("error", "Download failed: %s (%s)" % (str(title)[:50], message), episode_id=job.episode_id)
        return True

    def _retry(self, episode_id, keep, priority):
        row = self.store.one("SELECT status FROM downloads WHERE episode_id = ?", (episode_id,))
        if row is not None and row["status"] == "queued":
            self._enqueue(episode_id, keep, priority)
        elif row is None or row["status"] in ("error", "paused"):
            # Nothing will finish this one; do not leave the transcriber hanging.
            self._resolve_waiters(episode_id, None)
        # 'downloading' / 'done': a replacement started by request() during the
        # delay owns the waiters and settles them itself.

    # ---- the blocking transfer --------------------------------------------

    def _target_path(self, row, keep):
        ext = extension_for(row["enclosure_type"], row["enclosure_url"])
        if keep:
            folder = os.path.join(self.engine.settings.download_dir, safe_name(row["podcast_title"] or "Podcast", 80))
            date = time.strftime("%Y-%m-%d", time.gmtime(row["pub_date"])) if row["pub_date"] else "undated"
            base = "%s %s" % (date, safe_name(row["title"], 100))
        else:
            folder = self.engine.paths.audio_dir
            base = "%d-%s" % (row["id"], safe_name(row["title"], 60))
        os.makedirs(folder, exist_ok=True)
        taken = set()
        for other in self.store.all("SELECT path, temp_path FROM downloads WHERE episode_id != ?", (row["id"],)):
            for value in (other["path"], other["temp_path"]):
                if value:
                    taken.add(value)
        path = os.path.join(folder, base + "." + ext)
        counter = 2
        while os.path.exists(path) or path in taken or (path + ".part") in taken:
            path = os.path.join(folder, "%s (%d).%s" % (base, counter, ext))
            counter += 1
        return path

    def _plan(self, job):
        """Everything the transfer needs, read on the loop thread so the
        worker thread never touches SQLite."""
        row = self.store.one(models.EPISODE_SELECT + " WHERE e.id = ?", (job.episode_id,))
        if row is None:
            raise http.FetchError("bad-url", "episode vanished")
        stored = self.store.one("SELECT path, temp_path, etag, last_modified FROM downloads WHERE episode_id = ?", (job.episode_id,))
        path = stored["path"] if stored and stored["path"] else self._target_path(row, job.keep)
        # The partial file's name is unpredictable and remembered in the row,
        # so a resume finds it again while nobody else can guess it.
        part = stored["temp_path"] if stored and stored["temp_path"] else "%s.%s.part" % (path, secrets.token_hex(6))
        self.store.execute("UPDATE downloads SET path = ?, temp_path = ? WHERE episode_id = ?", (path, part, job.episode_id))
        return {
            "url": row["enclosure_url"], "path": path, "part": part,
            "expected": row["enclosure_length"],
            "validator": (stored["etag"] if stored else None) or (stored["last_modified"] if stored else None),
            "podcast_artwork": row["podcast_artwork_path"], "keep": job.keep,
        }

    def _download(self, job, plan):
        """Blocking transfer; runs in the pool. Returns the final path and
        fills plan["etag"] / plan["last_modified"] for the caller to store."""
        url, path, part = plan["url"], plan["path"], plan["part"]
        self._check_space(path, plan["expected"])

        resume_from = 0
        try:
            resume_from = os.path.getsize(part)
        except OSError:
            resume_from = 0
        headers = {}
        if resume_from > 0:
            headers["Range"] = "bytes=%d-" % resume_from
            if plan.get("validator"):
                headers["If-Range"] = plan["validator"]

        response = http.open_stream(url, timeout=30, headers=headers)
        try:
            status = getattr(response, "status", 200)
            total_header = response.headers.get("Content-Length")
            content_range = response.headers.get("Content-Range") or ""
            if status == 206 and content_range.startswith("bytes ") and content_range.split(" ")[1].split("-")[0] == str(resume_from):
                mode = "ab"
                job.bytes_done = resume_from
                total = int(content_range.split("/")[-1]) if "/" in content_range and content_range.split("/")[-1].isdigit() else None
            else:
                mode = "wb"
                job.bytes_done = 0
                total = int(total_header) if total_header and total_header.isdigit() else None
            job.bytes_total = total
            plan["etag"] = response.headers.get("ETag")
            plan["last_modified"] = response.headers.get("Last-Modified")
            expected = total or int(plan.get("expected") or 0)
            cap = max(expected * SIZE_SLACK, ABSOLUTE_CAP if not expected else 0) or ABSOLUTE_CAP
            last_rate_at = time.monotonic()
            last_rate_bytes = job.bytes_done
            last_progress_at = last_rate_at
            with os.fdopen(_open_part(part, mode), mode) as handle:
                while True:
                    if job.cancel.is_set():
                        raise Cancelled()
                    chunk = http.read_chunk(response)
                    if not chunk:
                        break
                    handle.write(chunk)
                    job.bytes_done += len(chunk)
                    if job.bytes_done > cap:
                        raise http.FetchError("too-large", "the file is far larger than the feed said")
                    stamp = time.monotonic()
                    if stamp - last_rate_at >= 1.0:
                        job.rate = (job.bytes_done - last_rate_bytes) / (stamp - last_rate_at)
                        last_rate_at, last_rate_bytes = stamp, job.bytes_done
                        if job.rate < 256 and stamp - last_progress_at > STALL_SECONDS:
                            raise http.FetchError("network", "the server stopped sending")
                        if job.rate >= 256:
                            last_progress_at = stamp
                    self.engine.call_soon(self._progress, job)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            response.close()
        if job.bytes_total and job.bytes_done != job.bytes_total:
            raise http.FetchError("network", "connection dropped at %d of %d bytes" % (job.bytes_done, job.bytes_total))
        os.replace(part, path)
        return path

    def _check_space(self, path, expected):
        try:
            usage = shutil.disk_usage(os.path.dirname(path))
        except OSError:
            return
        need = int(expected or 0) * FREE_SPACE_FACTOR or 200 * 1024 * 1024
        if usage.free < need:
            raise http.FetchError("network", "not enough free space (%d MB left)" % (usage.free // (1024 * 1024)))

    def _cover_for(self, plan):
        """Drop a cover.jpg next to a podcast's files once, for file managers."""
        if not plan.get("keep"):
            return
        try:
            folder = os.path.dirname(plan["path"])
            if os.path.exists(os.path.join(folder, "cover.jpg")):
                return
            art = plan.get("podcast_artwork")
            if art and os.path.exists(art) and folder.startswith(self.engine.settings.download_dir):
                shutil.copyfile(art, os.path.join(folder, "cover.jpg"))
        except OSError:
            pass

    async def _promote(self, episode_id, cache_path):
        """A cache-only file the user now wants to keep: move it into the
        library. The move may be a copy across filesystems, so it runs off
        the loop thread."""
        row = self.store.one(models.EPISODE_SELECT + " WHERE e.id = ?", (episode_id,))
        if row is None:
            return
        target = self._target_path(row, 1)
        # Reserve the path now so a concurrent download cannot pick it.
        self.store.execute("UPDATE downloads SET keep = 1, path = ? WHERE episode_id = ?", (target, episode_id))

        def move():
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(cache_path, target)

        try:
            await self.engine.run_in_thread(move)
        except OSError as error:
            LOG.warning("could not move %s into the library: %s", cache_path, error)
            self.store.execute("UPDATE downloads SET keep = 0, path = ? WHERE episode_id = ?", (cache_path, episode_id))
            return
        self.engine.library.record_action(episode_id, "download")
        self.broadcast()
        self.engine.library.emit_episode(episode_id)

    # ---- policy ------------------------------------------------------------

    def _on_new_episodes(self, podcast_id, episode_ids, first_fetch):
        if first_fetch:
            return
        podcast = self.store.one("SELECT auto_download FROM podcasts WHERE id = ?", (podcast_id,))
        policy = (podcast["auto_download"] if podcast and podcast["auto_download"] else None) or self.engine.settings.autoDownload
        if policy == "none" or not episode_ids:
            return
        rows = self.store.all(
            "SELECT id FROM episodes WHERE id IN (%s) ORDER BY pub_date DESC, id DESC" % ",".join("?" * len(episode_ids)),
            [int(e) for e in episode_ids])
        wanted = [r["id"] for r in rows]
        if policy == "latest":
            wanted = wanted[:1]
        if wanted:
            self.request(wanted, keep=1, priority=PRIORITY_AUTO)

    def cleanup(self):
        """Housekeeping: played episodes past their keep window, stale cache
        audio, and abandoned .part files."""
        freed = 0
        days = int(self.engine.settings.deletePlayedAfterDays)
        keep_per = int(self.engine.settings.keepDownloads)
        removed = []
        if days > 0:
            cutoff = now() - days * 86400
            for row in self.store.all(
                    "SELECT d.episode_id, d.path FROM downloads d JOIN episodes e ON e.id = d.episode_id "
                    "LEFT JOIN queue q ON q.episode_id = e.id "
                    "WHERE d.status = 'done' AND d.keep = 1 AND e.played = 1 AND e.played_at IS NOT NULL AND e.played_at < ? AND q.episode_id IS NULL",
                    (cutoff,)):
                freed += _remove(row["path"])
                self.store.execute("DELETE FROM downloads WHERE episode_id = ?", (row["episode_id"],))
                removed.append(row["episode_id"])
        # Per-podcast cap on kept downloads (oldest played first, then oldest unplayed).
        for podcast in self.store.all("SELECT id FROM podcasts"):
            rows = self.store.all(
                "SELECT d.episode_id, d.path, e.played FROM downloads d JOIN episodes e ON e.id = d.episode_id "
                "LEFT JOIN queue q ON q.episode_id = e.id "
                "WHERE e.podcast_id = ? AND d.status = 'done' AND d.keep = 1 AND q.episode_id IS NULL ORDER BY e.played DESC, e.pub_date ASC",
                (podcast["id"],))
            excess = len(rows) - keep_per
            for row in rows[:max(0, excess)]:
                if not row["played"]:
                    break
                freed += _remove(row["path"])
                self.store.execute("DELETE FROM downloads WHERE episode_id = ?", (row["episode_id"],))
                removed.append(row["episode_id"])
        # Cache audio (fetched only to transcribe) and abandoned partial files.
        stamp = now()
        for row in self.store.all("SELECT episode_id, path, finished_at FROM downloads WHERE keep = 0 AND status = 'done'"):
            if row["finished_at"] and stamp - row["finished_at"] > CACHE_MAX_AGE:
                freed += _remove(row["path"])
                self.store.execute("DELETE FROM downloads WHERE episode_id = ?", (row["episode_id"],))
                removed.append(row["episode_id"])
        for row in self.store.all("SELECT episode_id, temp_path, created_at FROM downloads WHERE status IN ('paused', 'error')"):
            if row["temp_path"] and stamp - row["created_at"] > PART_MAX_AGE:
                freed += _remove(row["temp_path"])
                self.store.execute("DELETE FROM downloads WHERE episode_id = ?", (row["episode_id"],))
                removed.append(row["episode_id"])
        for episode_id in removed:
            self.engine.library.emit_episode(episode_id)
        if removed:
            self.broadcast()
            LOG.info("cleanup removed %d file(s), %d MB", len(removed), freed // (1024 * 1024))
            if freed > 100 * 1024 * 1024:
                self.engine.notice("info", "Cleaned up %d MB of played downloads" % (freed // (1024 * 1024)))
        return {"removed": len(removed), "freed": freed}


def _open_part(part, mode):
    """Open a partial file for a fresh ("wb") or resumed ("ab") transfer
    without ever following a symlink standing in its place."""
    if mode == "wb":
        try:
            os.unlink(part)
        except FileNotFoundError:
            pass
        return fsio.open_nofollow(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    return fsio.open_nofollow(part, os.O_WRONLY | os.O_APPEND)


def _remove(path):
    try:
        size = os.path.getsize(path)
        os.unlink(path)
        return size
    except OSError:
        return 0


# ---------------------------------------------------------------- commands

@protocol.command("download", "Keep episodes on disk", episodeIds=A("int-list"))
def cmd_download(engine, client, episodeIds):
    return engine.downloads.request(episodeIds, keep=1, priority=PRIORITY_USER)


@protocol.command("download-cancel", "Stop downloading (keeps the partial file for a resume)", episodeIds=A("int-list"))
def cmd_download_cancel(engine, client, episodeIds):
    return engine.downloads.cancel(episodeIds)


@protocol.command("download-delete", "Remove downloaded files", episodeIds=A("int-list"))
def cmd_download_delete(engine, client, episodeIds):
    return engine.downloads.delete(episodeIds)


@protocol.command("downloads-cleanup", "Run the storage cleanup now")
def cmd_downloads_cleanup(engine, client):
    return engine.downloads.cleanup()
