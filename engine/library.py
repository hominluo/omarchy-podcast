"""Subscriptions and episodes: fetching feeds, storing them, and answering the
library, inbox and history queries.

Network work runs in the shared thread pool; every database write happens on
the loop thread inside one transaction per feed. After a refresh the library
and inbox slices are rebroadcast and `episodes-changed` tells open views which
podcast to reload.
"""

import asyncio
import json
import os
import random
import sqlite3
import time

from . import feeds, http, log, models, protocol
from .search import canonical_feed_url
from .store import now

LOG = log.get("library")

A = protocol.Arg
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
REFRESH_CONCURRENCY = 4
INBOX_PAGE = 50
PREVIEW_CACHE_SECONDS = 15 * 60
PREVIEW_CACHE_SIZE = 12   # parsed feeds are kept too, so keep this small


def _checked_url(feed_url):
    """User-typed links: a bad one is a bad request, not an internal error."""
    try:
        return http.check_url(feed_url)
    except http.FetchError as error:
        raise protocol.ProtocolError(protocol.BAD_REQUEST, error.message)


class Library:
    def __init__(self, engine):
        self.engine = engine
        self.store = None
        self._refreshing = set()
        self._previews = {}           # feed url -> (monotonic stamp, (podcast, episodes))
        self._semaphore = None
        self._jobs_total = 0
        self._jobs_done = 0
        # Set by the playback subsystem so refreshes can hand new episodes on.
        self.on_new_episodes = None

    async def start(self):
        self.store = self.engine.store
        self._semaphore = asyncio.Semaphore(REFRESH_CONCURRENCY)
        self.broadcast_library()
        self.broadcast_inbox()

    async def stop(self, restart=False, quit_mpv=True):
        pass

    # ---- broadcasts --------------------------------------------------------

    def library_list(self):
        rows = self.store.all(models.PODCAST_SELECT + " ORDER BY p.title COLLATE NOCASE")
        return [models.podcast_summary(row) for row in rows]

    def broadcast_library(self):
        self.engine.set_state("library", self.library_list())

    def inbox_page(self, offset=0, limit=INBOX_PAGE):
        rows = self.store.all(
            models.EPISODE_SELECT + " WHERE e.state = 'inbox' AND e.played = 0"
            " ORDER BY e.pub_date DESC, e.id DESC LIMIT ? OFFSET ?", (limit, offset))
        count = self.store.scalar("SELECT COUNT(*) FROM episodes WHERE state = 'inbox' AND played = 0", default=0)
        return {"count": count, "offset": offset, "items": [models.episode_summary(row) for row in rows]}

    def broadcast_inbox(self):
        self.engine.set_state("inbox", self.inbox_page(0, INBOX_PAGE))

    def episode_row(self, episode_id):
        return self.store.one(models.EPISODE_SELECT + " WHERE e.id = ?", (int(episode_id),))

    def require_episode(self, episode_id):
        row = self.episode_row(episode_id)
        if row is None:
            raise protocol.ProtocolError(protocol.NOT_FOUND, "no episode %s" % episode_id)
        return row

    def podcast_row(self, podcast_id):
        return self.store.one(models.PODCAST_SELECT + " WHERE p.id = ?", (int(podcast_id),))

    def require_podcast(self, podcast_id):
        row = self.podcast_row(podcast_id)
        if row is None:
            raise protocol.ProtocolError(protocol.NOT_FOUND, "no podcast %s" % podcast_id)
        return row

    def emit_episode(self, episode_id):
        row = self.episode_row(episode_id)
        if row is not None:
            self.engine.emit("episode", models.episode_summary(row))
        return row

    # ---- fetching ----------------------------------------------------------

    def _fetch_and_parse(self, feed_url, etag=None, last_modified=None):
        response = http.fetch(feed_url, cap=http.FEED_CAP, etag=etag, last_modified=last_modified, accept=FEED_ACCEPT)
        parsed = feeds.parse(response.body, feed_url)
        return parsed, response

    async def fetch_feed(self, feed_url, etag=None, last_modified=None):
        return await self.engine.run_in_thread(self._fetch_and_parse, feed_url, etag, last_modified)

    # ---- subscribe / unsubscribe -------------------------------------------

    async def preview(self, feed_url):
        """A feed's show page without subscribing: podcast fields plus the
        latest episodes. Browsing flips between shows, so a fetched feed is
        kept for a while; the subscribed flag is always fresh."""
        url = _checked_url(feed_url)
        cached = self._previews.get(url)
        if cached is not None and time.monotonic() - cached[0] < PREVIEW_CACHE_SECONDS:
            podcast, episodes = cached[1]
        else:
            try:
                parsed, response = await self.fetch_feed(url)
            except http.FetchError as error:
                raise protocol.ProtocolError(protocol.NETWORK, error.message)
            except feeds.FeedParseError as error:
                raise protocol.ProtocolError(protocol.BAD_REQUEST, str(error))
            podcast = dict(parsed["podcast"])
            podcast["episodeCount"] = len(parsed["episodes"])
            episodes = []
            for ep in parsed["episodes"][:20]:
                episodes.append({
                    "title": ep["title"], "pubDate": ep["pub_date"], "duration": ep["duration"],
                    "enclosureUrl": ep["enclosure_url"], "notesText": ep["notes_text"][:220],
                    "artwork": ep["image_url"] or podcast.get("image_url", ""),
                })
            if len(self._previews) >= PREVIEW_CACHE_SIZE:
                oldest = min(self._previews, key=lambda key: self._previews[key][0])
                del self._previews[oldest]
            # The parsed feed and its response stay too: subscribing right
            # after a preview must not download the whole feed again.
            self._previews[url] = (time.monotonic(), (podcast, episodes), (parsed, response))
        podcast = dict(podcast)
        existing = self._podcast_by_url(url)
        podcast["subscribed"] = existing is not None
        podcast["podcastId"] = existing["id"] if existing else None
        return {"podcast": podcast, "episodes": list(episodes)}

    async def subscribe(self, feed_url):
        url = _checked_url(feed_url)
        existing = self._podcast_by_url(url)
        if existing is not None:
            return models.podcast_summary(self.require_podcast(existing["id"]))
        cached = self._previews.get(url)
        if cached is not None and time.monotonic() - cached[0] < PREVIEW_CACHE_SECONDS:
            parsed, response = cached[2]
        else:
            try:
                parsed, response = await self.fetch_feed(url)
            except http.FetchError as error:
                raise protocol.ProtocolError(protocol.NETWORK, error.message)
            except feeds.FeedParseError as error:
                raise protocol.ProtocolError(protocol.BAD_REQUEST, str(error))

        # The fetch took a while: a double click or a sync pull may have
        # subscribed meanwhile, and a redirect may have landed on a feed that
        # is already in the library under its final URL.
        final_url = response.url or url
        existing = self._podcast_by_url(url) or self._podcast_by_url(final_url)
        if existing is not None:
            return models.podcast_summary(self.require_podcast(existing["id"]))
        podcast = parsed["podcast"]
        stamp = now()
        try:
            with self.store.transaction():
                cursor = self.store.execute(
                    """INSERT INTO podcasts (feed_url, title, author, description_html, description_text, link, language,
                                            image_url, podcast_guid, funding_json, subscribed_at, etag, last_modified,
                                            last_fetch_at, last_fetch_ok, next_refresh_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (final_url, podcast["title"] or final_url, podcast["author"], podcast["description_html"],
                     podcast["description_text"], podcast["link"], podcast["language"], podcast["image_url"],
                     podcast["podcast_guid"], json.dumps(podcast["funding"]), stamp, response.etag, response.last_modified,
                     stamp, self._next_refresh_at(stamp)))
                podcast_id = cursor.lastrowid
                result = self._upsert_episodes(podcast_id, parsed["episodes"], first_fetch=True)
                self.store.execute(
                    "INSERT INTO subscription_changes (feed_url, action, timestamp) VALUES (?, 'add', ?)", (final_url, stamp))
        except sqlite3.IntegrityError:
            existing = self._podcast_by_url(final_url) or self._podcast_by_url(url)
            if existing is None:
                raise
            return models.podcast_summary(self.require_podcast(existing["id"]))
        LOG.info("subscribed to %s (%d episodes)", podcast["title"], len(parsed["episodes"]))
        artwork = getattr(self.engine, "artwork", None)
        if artwork is not None:
            asyncio.ensure_future(artwork.ensure_podcast(podcast_id))
        self.broadcast_library()
        self.broadcast_inbox()
        self.engine.emit("episodes-changed", {"podcastId": podcast_id, "added": result["added"], "updated": result["updated"]})
        self.engine.emit("subscribed", {"podcastId": podcast_id})
        self._after_fetch_hooks(podcast_id, result, first_fetch=True)
        self._settled()
        return models.podcast_summary(self.require_podcast(podcast_id))

    def _podcast_by_url(self, url):
        row = self.store.one("SELECT id FROM podcasts WHERE feed_url = ?", (url,))
        if row is not None:
            return row
        key = canonical_feed_url(url)
        for candidate in self.store.all("SELECT id, feed_url FROM podcasts"):
            if canonical_feed_url(candidate["feed_url"]) == key:
                return candidate
        return None

    def unsubscribe(self, podcast_id, delete_downloads=False):
        row = self.require_podcast(podcast_id)
        episode_ids = [r["id"] for r in self.store.all("SELECT id FROM episodes WHERE podcast_id = ?", (int(podcast_id),))]
        # Files to drop: kept downloads only when asked, cache audio, partial
        # files and transcript documents always.
        doomed = []
        for r in self.store.all(
                "SELECT d.path, d.temp_path, d.keep FROM downloads d JOIN episodes e ON e.id = d.episode_id WHERE e.podcast_id = ?",
                (int(podcast_id),)):
            if r["temp_path"]:
                doomed.append(r["temp_path"])
            if r["path"] and (delete_downloads or not r["keep"]):
                doomed.append(r["path"])
        for r in self.store.all(
                "SELECT t.path FROM transcripts t JOIN episodes e ON e.id = t.episode_id WHERE e.podcast_id = ? AND t.path != ''",
                (int(podcast_id),)):
            doomed.append(r["path"])
        downloads = getattr(self.engine, "downloads", None)
        if downloads is not None and episode_ids:
            downloads.cancel(episode_ids, quiet=True)
        transcripts = getattr(self.engine, "transcripts", None)
        if transcripts is not None:
            for episode_id in episode_ids:
                transcripts.forget(episode_id)
        playback = getattr(self.engine, "playback", None)
        if playback is not None and playback.current_row is not None and playback.current_row["podcast_id"] == int(podcast_id):
            asyncio.ensure_future(playback.stop_playback(clear=True))
        with self.store.transaction():
            self.store.execute("DELETE FROM podcasts WHERE id = ?", (int(podcast_id),))
            self.store.execute(
                "INSERT INTO subscription_changes (feed_url, action, timestamp) VALUES (?, 'remove', ?)",
                (row["feed_url"], now()))
        removed = 0
        for path in doomed:
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass
        self.broadcast_library()
        self.broadcast_inbox()
        queue = getattr(self.engine, "queue", None)
        if queue is not None:
            queue.broadcast()
        if downloads is not None:
            downloads.broadcast()
        self.engine.emit("unsubscribed", {"podcastId": int(podcast_id)})
        self._settled()
        return {"podcastId": int(podcast_id), "deletedFiles": removed}

    def update_podcast(self, podcast_id, **fields):
        self.require_podcast(podcast_id)
        columns = {
            "speed": "speed", "autoDownload": "auto_download", "skipIntroSec": "skip_intro_sec",
            "skipOutroSec": "skip_outro_sec", "autoQueue": "auto_queue",
        }
        sets, values = [], []
        for key, column in columns.items():
            if key in fields and fields[key] is not None:
                value = fields[key]
                if key == "speed":
                    value = None if value == 0 else float(value)
                elif key in ("skipIntroSec", "skipOutroSec"):
                    value = None if int(value) < 0 else int(value)
                elif key == "autoDownload":
                    value = None if value in ("", "inherit") else str(value)
                sets.append("%s = ?" % column)
                values.append(value)
        if sets:
            values.append(int(podcast_id))
            self.store.execute("UPDATE podcasts SET %s WHERE id = ?" % ", ".join(sets), values)
            self.broadcast_library()
            playback = getattr(self.engine, "playback", None)
            if playback is not None and playback.current_row is not None and playback.current_row["podcast_id"] == int(podcast_id):
                # nowPlaying carries the podcast's speed override.
                playback.broadcast_now_playing()
                if "speed" in fields and playback.loaded:
                    asyncio.ensure_future(playback._apply_speed())
        return models.podcast_detail(self.require_podcast(podcast_id))

    # ---- refresh -----------------------------------------------------------

    def _next_refresh_at(self, stamp, fail_count=0):
        interval = max(5, int(self.engine.settings.refreshIntervalMin)) * 60
        if fail_count:
            interval = min(24 * 3600, interval * (2 ** min(fail_count, 5)))
        return stamp + interval + random.randint(0, max(1, interval // 10))

    async def refresh(self, podcast_id=None, force=True):
        """Refresh one podcast, or every one that is due (all, when forced).
        Overlapping calls share one progress counter so the jobs slice only
        reports idle once the last of them is done."""
        if podcast_id is not None:
            self.require_podcast(podcast_id)
            ids = [int(podcast_id)]
        elif force:
            ids = [row["id"] for row in self.store.all("SELECT id FROM podcasts ORDER BY last_fetch_at")]
        else:
            ids = [row["id"] for row in self.store.all(
                "SELECT id FROM podcasts WHERE next_refresh_at <= ? ORDER BY next_refresh_at", (now(),))]
        ids = [pid for pid in ids if pid not in self._refreshing]
        if not ids:
            return {"refreshed": 0}
        for pid in ids:
            self._refreshing.add(pid)
        self._jobs_total += len(ids)
        self._set_jobs()
        added_total = 0

        async def one(pid):
            nonlocal added_total
            # The finally also covers a cancel while waiting for the semaphore
            # (refresh_podcast's own cleanup never runs in that case).
            try:
                async with self._semaphore:
                    result = await self.refresh_podcast(pid)
                    added_total += len(result.get("added", []))
            finally:
                self._refreshing.discard(pid)
                self._jobs_done += 1
                self._set_jobs()

        try:
            await asyncio.gather(*(one(pid) for pid in ids), return_exceptions=True)
        finally:
            if self._jobs_done >= self._jobs_total:
                self._jobs_total = self._jobs_done = 0
                self._set_jobs()
        self.broadcast_library()
        self.broadcast_inbox()
        return {"refreshed": len(ids), "added": added_total}

    def _set_jobs(self):
        self.engine.update_state("jobs", refreshing=self._jobs_done < self._jobs_total,
                                 refreshTotal=self._jobs_total, refreshDone=self._jobs_done)

    async def refresh_podcast(self, podcast_id):
        row = self.store.one("SELECT * FROM podcasts WHERE id = ?", (int(podcast_id),))
        if row is None:
            self._refreshing.discard(int(podcast_id))
            return {"added": [], "updated": 0}
        self._refreshing.add(row["id"])
        stamp = now()
        try:
            try:
                parsed, response = await self.fetch_feed(row["feed_url"], row["etag"], row["last_modified"])
            except http.NotModified:
                self.store.execute(
                    "UPDATE podcasts SET last_fetch_at = ?, last_fetch_ok = 1, last_error = '', fail_count = 0, next_refresh_at = ? WHERE id = ?",
                    (stamp, self._next_refresh_at(stamp), row["id"]))
                return {"added": [], "updated": 0, "notModified": True}
            except (http.FetchError, feeds.FeedParseError) as error:
                message = getattr(error, "message", None) or str(error)
                fails = int(row["fail_count"] or 0) + 1
                self.store.execute(
                    "UPDATE podcasts SET last_fetch_at = ?, last_fetch_ok = 0, last_error = ?, fail_count = ?, next_refresh_at = ? WHERE id = ?",
                    (stamp, message[:200], fails, self._next_refresh_at(stamp, fails), row["id"]))
                LOG.warning("refresh of %s failed: %s", row["title"], message)
                return {"added": [], "updated": 0, "error": message}
            except Exception as error:  # noqa: BLE001 - keep the loop alive whatever a feed does
                LOG.exception("refresh of %s crashed", row["title"])
                self.store.execute(
                    "UPDATE podcasts SET last_fetch_at = ?, last_fetch_ok = 0, last_error = ?, fail_count = fail_count + 1, next_refresh_at = ? WHERE id = ?",
                    (stamp, ("%s: %s" % (type(error).__name__, error))[:200], self._next_refresh_at(stamp, int(row["fail_count"] or 0) + 1), row["id"]))
                return {"added": [], "updated": 0, "error": str(error)}

            podcast = parsed["podcast"]
            with self.store.transaction():
                self.store.execute(
                    """UPDATE podcasts SET title = ?, author = ?, description_html = ?, description_text = ?, link = ?,
                           language = ?, image_url = ?, podcast_guid = COALESCE(?, podcast_guid), funding_json = ?,
                           etag = ?, last_modified = ?, last_fetch_at = ?, last_fetch_ok = 1, last_error = '',
                           fail_count = 0, next_refresh_at = ?
                       WHERE id = ?""",
                    (podcast["title"] or row["title"], podcast["author"], podcast["description_html"],
                     podcast["description_text"], podcast["link"], podcast["language"],
                     podcast["image_url"] or row["image_url"], podcast["podcast_guid"], json.dumps(podcast["funding"]),
                     response.etag, response.last_modified, stamp, self._next_refresh_at(stamp), row["id"]))
                result = self._upsert_episodes(row["id"], parsed["episodes"], first_fetch=False)
            if result["added"] or result["updated"]:
                self.engine.emit("episodes-changed", {"podcastId": row["id"], "added": result["added"], "updated": result["updated"]})
            self._after_fetch_hooks(row["id"], result, first_fetch=False)
            return result
        finally:
            self._refreshing.discard(row["id"])

    def _upsert_episodes(self, podcast_id, episodes, first_fetch):
        """Insert new items, update changed ones (by content hash). Runs inside
        the caller's transaction. Returns {"added": [ids], "updated": n}."""
        known = {}
        for r in self.store.all("SELECT id, guid, content_hash FROM episodes WHERE podcast_id = ?", (podcast_id,)):
            known[r["guid"]] = (r["id"], r["content_hash"])
        stamp = now()
        added, updated = [], 0
        inbox_budget = max(0, int(self.engine.settings.initialInboxCount)) if first_fetch else None
        if first_fetch:
            # Feeds list newest-first almost always, but not always; the
            # inbox budget must go to the newest episodes regardless.
            episodes = sorted(episodes, key=lambda ep: -(ep["pub_date"] or 0))
        for ep in episodes:
            existing = known.get(ep["guid"])
            values = (
                ep["title"], ep["link"], ep["pub_date"], ep["duration"], ep["enclosure_url"], ep["enclosure_type"],
                ep["enclosure_length"], ep["notes_html"], ep["notes_text"], ep["image_url"], ep["episode_number"],
                ep["season"], ep["episode_type"], 1 if ep["explicit"] else 0, ep["chapters_url"], ep["chapters_type"],
                json.dumps(ep["transcripts"]), json.dumps(ep["persons"]), ep["content_hash"],
            )
            if existing is None:
                state = "inbox"
                if inbox_budget is not None:
                    if inbox_budget > 0:
                        inbox_budget -= 1
                    else:
                        state = "archived"
                cursor = self.store.execute(
                    """INSERT INTO episodes (podcast_id, guid, title, link, pub_date, duration, enclosure_url, enclosure_type,
                                             enclosure_length, notes_html, notes_text, image_url, episode_number, season,
                                             episode_type, explicit, chapters_url, chapters_type, transcripts_json,
                                             persons_json, content_hash, state, first_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (podcast_id, ep["guid"]) + values + (state, stamp))
                added.append(cursor.lastrowid)
            elif existing[1] != ep["content_hash"]:
                self.store.execute(
                    """UPDATE episodes SET title = ?, link = ?, pub_date = ?, duration = ?, enclosure_url = ?, enclosure_type = ?,
                           enclosure_length = ?, notes_html = ?, notes_text = ?, image_url = ?, episode_number = ?, season = ?,
                           episode_type = ?, explicit = ?, chapters_url = ?, chapters_type = ?, transcripts_json = ?,
                           persons_json = ?, content_hash = ?
                       WHERE id = ?""",
                    values + (existing[0],))
                updated += 1
        return {"added": added, "updated": updated}

    def _settled(self):
        hook = getattr(self.engine, "on_playback_settled", None)
        if hook:
            hook()

    def _after_fetch_hooks(self, podcast_id, result, first_fetch):
        if result.get("added") and self.on_new_episodes:
            try:
                self.on_new_episodes(podcast_id, list(result["added"]), first_fetch)
            except Exception:  # noqa: BLE001
                LOG.exception("new-episode hook failed")

    # ---- queries -----------------------------------------------------------

    def episodes_page(self, podcast_id, offset=0, limit=100, filter_name="all", sort="newest"):
        self.require_podcast(podcast_id)
        where = ["e.podcast_id = ?"]
        params = [int(podcast_id)]
        if filter_name == "unplayed":
            where.append("e.played = 0")
        elif filter_name == "downloaded":
            where.append("d.status = 'done'")
        elif filter_name == "inbox":
            where.append("e.state = 'inbox' AND e.played = 0")
        order = "e.pub_date ASC, e.id ASC" if sort == "oldest" else "e.pub_date DESC, e.id DESC"
        clause = " WHERE " + " AND ".join(where)
        total = self.store.scalar(
            "SELECT COUNT(*) FROM episodes e LEFT JOIN downloads d ON d.episode_id = e.id" + clause, params, default=0)
        rows = self.store.all(models.EPISODE_SELECT + clause + " ORDER BY " + order + " LIMIT ? OFFSET ?",
                              params + [int(limit), int(offset)])
        return {"total": total, "offset": int(offset), "items": [models.episode_summary(row) for row in rows]}

    def history_page(self, offset=0, limit=100):
        rows = self.store.all(
            models.EPISODE_SELECT + " WHERE e.last_played_at IS NOT NULL ORDER BY e.last_played_at DESC LIMIT ? OFFSET ?",
            (int(limit), int(offset)))
        total = self.store.scalar("SELECT COUNT(*) FROM episodes WHERE last_played_at IS NOT NULL", default=0)
        return {"total": total, "offset": int(offset), "items": [models.episode_summary(row) for row in rows]}

    # ---- episode state -----------------------------------------------------

    def mark_played(self, episode_ids, played=True):
        stamp = now()
        changed = []
        for episode_id in episode_ids:
            row = self.store.one("SELECT id, duration, position FROM episodes WHERE id = ?", (int(episode_id),))
            if row is None:
                continue
            if played:
                self.store.execute(
                    "UPDATE episodes SET played = 1, played_at = ?, state = 'archived', position = COALESCE(duration, position) WHERE id = ?",
                    (stamp, row["id"]))
                self.store.execute("DELETE FROM queue WHERE episode_id = ?", (row["id"],))
            else:
                self.store.execute("UPDATE episodes SET played = 0, played_at = NULL, position = 0 WHERE id = ?", (row["id"],))
            self.record_action(row["id"], "play" if played else "new", position=row["duration"] if played else 0,
                               total=row["duration"], started=0)
            changed.append(row["id"])
        for episode_id in changed:
            self.emit_episode(episode_id)
        if changed:
            self.broadcast_inbox()
            self.broadcast_library()
            queue = getattr(self.engine, "queue", None)
            if queue is not None:
                queue.broadcast()
        return {"changed": changed}

    def set_state(self, episode_ids, state):
        changed = []
        for episode_id in episode_ids:
            cursor = self.store.execute("UPDATE episodes SET state = ? WHERE id = ?", (state, int(episode_id)))
            if cursor.rowcount:
                changed.append(int(episode_id))
        for episode_id in changed:
            self.emit_episode(episode_id)
        if changed:
            self.broadcast_inbox()
            self.broadcast_library()
        return {"changed": changed}

    def set_position(self, episode_id, position, flush_action=False):
        row = self.store.one("SELECT id, duration FROM episodes WHERE id = ?", (int(episode_id),))
        if row is None:
            return
        stamp = now()
        self.store.execute(
            "UPDATE episodes SET position = ?, position_updated_at = ?, last_played_at = ? WHERE id = ?",
            (float(position), stamp, stamp, row["id"]))
        if flush_action:
            self.record_action(row["id"], "play", position=position, total=row["duration"], started=0)

    def record_action(self, episode_id, action, position=None, total=None, started=None):
        row = self.store.one(
            "SELECT e.guid, e.enclosure_url, p.feed_url FROM episodes e JOIN podcasts p ON p.id = e.podcast_id WHERE e.id = ?",
            (int(episode_id),))
        if row is None:
            return
        self.store.execute(
            "INSERT INTO episode_actions (podcast_feed_url, episode_url, episode_guid, action, started, position, total, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row["feed_url"], row["enclosure_url"], row["guid"], action, started, position, total, now()))


# ---------------------------------------------------------------- commands

def _lib(engine):
    return engine.library


@protocol.command("feed-preview", "Fetch a feed without subscribing", feedUrl=A(str))
async def cmd_feed_preview(engine, client, feedUrl):
    return await _lib(engine).preview(feedUrl)


@protocol.command("subscribe", "Subscribe to a feed URL", feedUrl=A(str))
async def cmd_subscribe(engine, client, feedUrl):
    return {"podcast": await _lib(engine).subscribe(feedUrl)}


@protocol.command("unsubscribe", "Remove a podcast", podcastId=A(int), deleteDownloads=A(bool, required=False, default=False))
def cmd_unsubscribe(engine, client, podcastId, deleteDownloads):
    return _lib(engine).unsubscribe(podcastId, deleteDownloads)


@protocol.command("podcast-get", "One podcast with its description", podcastId=A(int))
def cmd_podcast_get(engine, client, podcastId):
    return {"podcast": models.podcast_detail(_lib(engine).require_podcast(podcastId))}


@protocol.command("podcast-update", "Per-podcast preferences", podcastId=A(int),
                  speed=A(float, required=False), autoDownload=A(str, required=False),
                  skipIntroSec=A(int, required=False), skipOutroSec=A(int, required=False),
                  autoQueue=A(str, required=False, choices=["none", "next", "last"]))
def cmd_podcast_update(engine, client, podcastId, **fields):
    return {"podcast": _lib(engine).update_podcast(podcastId, **fields)}


@protocol.command("episodes", "A page of one podcast's episodes", podcastId=A(int),
                  offset=A(int, required=False, default=0, minimum=0), limit=A(int, required=False, default=100, minimum=1, maximum=200),
                  filter=A(str, required=False, default="all", choices=["all", "unplayed", "downloaded", "inbox"]),
                  sort=A(str, required=False, default="newest", choices=["newest", "oldest"]))
def cmd_episodes(engine, client, podcastId, offset, limit, filter, sort):
    return _lib(engine).episodes_page(podcastId, offset, limit, filter, sort)


@protocol.command("episode-get", "Everything about one episode", episodeId=A(int))
def cmd_episode_get(engine, client, episodeId):
    row = _lib(engine).require_episode(episodeId)
    detail = models.episode_detail(row)
    chapters = getattr(engine, "chapters", None)
    if chapters is not None:
        cached = chapters.cached(int(episodeId))
        if cached is None and row["chapters_url"]:
            chapters._maybe_fetch(int(episodeId))
        detail["chapters"] = chapters.merge(int(episodeId), [], row["duration"]) if cached else []
    return {"episode": detail}


@protocol.command("episode-set-state", "Move episodes between inbox and archive",
                  episodeIds=A("int-list"), state=A(str, choices=["inbox", "archived"]))
def cmd_episode_set_state(engine, client, episodeIds, state):
    return _lib(engine).set_state(episodeIds, state)


@protocol.command("mark-played", "Mark episodes played or unplayed", episodeIds=A("int-list"), played=A(bool, required=False, default=True))
def cmd_mark_played(engine, client, episodeIds, played):
    return _lib(engine).mark_played(episodeIds, played)


@protocol.command("inbox", "New, unplayed episodes", offset=A(int, required=False, default=0, minimum=0),
                  limit=A(int, required=False, default=INBOX_PAGE, minimum=1, maximum=200))
def cmd_inbox(engine, client, offset, limit):
    return _lib(engine).inbox_page(offset, limit)


@protocol.command("history", "Recently played episodes", offset=A(int, required=False, default=0, minimum=0),
                  limit=A(int, required=False, default=100, minimum=1, maximum=200))
def cmd_history(engine, client, offset, limit):
    return _lib(engine).history_page(offset, limit)


@protocol.command("refresh", "Fetch feeds for new episodes", podcastId=A(int, required=False))
async def cmd_refresh(engine, client, podcastId):
    return await _lib(engine).refresh(podcastId, force=True)
