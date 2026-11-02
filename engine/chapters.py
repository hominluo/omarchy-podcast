"""Chapters from two sources, merged into one list.

mpv reports the chapters embedded in the audio (ID3 CHAP frames, MP4
chapter tracks). Podcasting 2.0 feeds link a JSON file instead, which is
editable after publishing and often the only source. Both are kept: JSON
titles win where the two agree within a couple of seconds, hidden (`toc:
false`) entries are dropped, and the result is sorted with end times filled
in so the seek bar can draw notches and the transcript can show the current
chapter.
"""

import asyncio
import json
import time

from . import http, log
from .store import now

LOG = log.get("chapters")

PROXIMITY = 2.0
MAX_CHAPTERS = 500
# A fetch that failed (or a document with no chapters) is retried after this
# long. Failed fetches are not written to the database, so a feed that was
# down for a minute does not lose its chapters until the app is reinstalled.
RETRY_SECONDS = 15 * 60


def parse_json_chapters(data):
    """Podcasting 2.0 chapters document -> [{startTime, endTime, title, img, url}]."""
    try:
        payload = json.loads(data.decode("utf-8", "replace") if isinstance(data, bytes) else data)
    except ValueError:
        return []
    items = payload.get("chapters") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    chapters = []
    for item in items[:MAX_CHAPTERS]:
        if not isinstance(item, dict):
            continue
        if item.get("toc") is False:
            continue
        try:
            start = float(item.get("startTime"))
        except (TypeError, ValueError):
            continue
        if start < 0:
            continue
        end = item.get("endTime")
        try:
            end = float(end) if end is not None else None
        except (TypeError, ValueError):
            end = None
        url = str(item.get("url") or "")
        chapters.append({
            "startTime": start,
            "endTime": end,
            "title": str(item.get("title") or "").strip(),
            "img": str(item.get("img") or "") if str(item.get("img") or "").startswith(("http://", "https://")) else "",
            "url": url if url.startswith(("http://", "https://")) else "",
        })
    chapters.sort(key=lambda c: c["startTime"])
    return chapters


def merge(embedded, external, duration=None):
    """Combine mpv's chapters with the feed's. Feed titles win on collisions."""
    merged = []
    for item in external or []:
        merged.append(dict(item))
    for item in embedded or []:
        start = float(item.get("startTime") or 0)
        twin = next((c for c in merged if abs(c["startTime"] - start) <= PROXIMITY), None)
        if twin is not None:
            if not twin.get("title") and item.get("title"):
                twin["title"] = item["title"]
            continue
        merged.append({"startTime": start, "endTime": item.get("endTime"), "title": str(item.get("title") or ""), "img": "", "url": ""})
    merged.sort(key=lambda c: c["startTime"])
    for index, chapter in enumerate(merged):
        chapter["index"] = index
        nxt = merged[index + 1]["startTime"] if index + 1 < len(merged) else duration
        if not chapter.get("endTime") or (nxt is not None and chapter["endTime"] > nxt + 0.5):
            chapter["endTime"] = nxt
        if not chapter.get("title"):
            chapter["title"] = "Chapter %d" % (index + 1)
    return merged


class Chapters:
    def __init__(self, engine):
        self.engine = engine
        self._fetching = set()
        self._memory = {}
        self._failed_at = {}

    async def start(self):
        pass

    async def stop(self, restart=False, quit_mpv=True):
        pass

    def cached(self, episode_id):
        episode_id = int(episode_id)
        if episode_id in self._memory:
            return self._memory[episode_id]
        row = self.engine.store.one("SELECT chapters_json, fetched_at FROM chapters_cache WHERE episode_id = ? AND source = 'pi-json'", (episode_id,))
        if row is None:
            return None
        try:
            chapters = json.loads(row["chapters_json"])
        except ValueError:
            chapters = []
        # Older releases cached a failed fetch as []; retry those once the
        # timestamp is old enough instead of showing an empty tab forever.
        if not chapters and now() - int(row["fetched_at"] or 0) >= RETRY_SECONDS:
            return None
        self._memory[episode_id] = chapters
        return chapters

    def merge(self, episode_id, embedded, duration):
        """Called by playback for the current episode. Kicks off a fetch of the
        feed's chapters when they are not cached yet; the broadcast that
        follows the fetch redraws the seek bar."""
        external = self.cached(episode_id)
        if external is None:
            self._maybe_fetch(episode_id)
            external = []
        return merge(embedded, external, duration)

    def _maybe_fetch(self, episode_id):
        if episode_id in self._fetching:
            return
        failed = self._failed_at.get(episode_id)
        if failed is not None and time.monotonic() - failed < RETRY_SECONDS:
            return
        row = self.engine.store.one("SELECT chapters_url FROM episodes WHERE id = ?", (int(episode_id),))
        if row is None or not row["chapters_url"]:
            return
        self._fetching.add(episode_id)
        asyncio.ensure_future(self._fetch(episode_id, row["chapters_url"]))

    async def _fetch(self, episode_id, url):
        try:
            chapters = None
            try:
                response = await self.engine.run_in_thread(http.fetch, url, http.SMALL_CAP, 20)
                chapters = parse_json_chapters(response.body)
                self._failed_at.pop(episode_id, None)
            except http.FetchError as error:
                # Keep the failure in memory only: writing [] here used to
                # cache a transient 404 for the life of the database.
                self._failed_at[episode_id] = time.monotonic()
                LOG.info("chapters for episode %d: %s", episode_id, error.message)
            if chapters is not None:
                self.engine.store.execute(
                    "INSERT INTO chapters_cache (episode_id, source, chapters_json, fetched_at) VALUES (?, 'pi-json', ?, ?) "
                    "ON CONFLICT(episode_id) DO UPDATE SET chapters_json = excluded.chapters_json, fetched_at = excluded.fetched_at",
                    (int(episode_id), json.dumps(chapters), now()))
                self._memory[episode_id] = chapters
            playback = getattr(self.engine, "playback", None)
            if playback is not None and playback.current_id == episode_id:
                playback.broadcast_now_playing()
            self.engine.library.emit_episode(episode_id)
        finally:
            self._fetching.discard(episode_id)
