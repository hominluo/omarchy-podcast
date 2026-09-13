"""Sync subscriptions and playback positions with gpodder.net or the
Nextcloud GPodder Sync app.

Both speak the gpodder.net API 2 shapes; Nextcloud drops the device layer
and keeps only the latest action per episode. Locally, every subscribe,
unsubscribe and play/pause lands in `subscription_changes` and
`episode_actions` with synced=0; a cycle pushes those, then pulls what other
clients did since the last timestamp and applies it. Positions are settled
by timestamp: the newer one wins.
"""

import asyncio
import base64
import datetime
import json
import sqlite3
import time
import urllib.parse

from . import http, log, protocol
from .search import canonical_feed_url
from .store import now

LOG = log.get("sync")
A = protocol.Arg

BATCH = 500
DEBOUNCE_SECONDS = 30
PLAYED_MARGIN = 30


class SyncError(Exception):
    pass


def _iso(stamp):
    return datetime.datetime.fromtimestamp(int(stamp), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _from_iso(text):
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    text = str(text).strip()
    if text.isdigit():
        return int(text)
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp())


class Client:
    """HTTP client for one provider. Blocking; called from the pool."""

    def __init__(self, provider, server, username, password, device_id):
        self.provider = provider
        self.server = server.rstrip("/")
        if not self.server.startswith(("http://", "https://")):
            self.server = "https://" + self.server
        self.username = username
        self.password = password
        self.device_id = device_id
        if provider == "nextcloud":
            self.base = self.server + "/index.php/apps/gpoddersync"
        else:
            self.base = self.server

    def _headers(self):
        token = base64.b64encode(("%s:%s" % (self.username, self.password)).encode("utf-8")).decode("ascii")
        headers = {"Authorization": "Basic " + token, "Accept": "application/json"}
        if self.provider == "nextcloud":
            headers["OCS-APIRequest"] = "true"
        return headers

    def _call(self, method, path, params=None, body=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            response = http.fetch(url, cap=http.SMALL_CAP * 4, timeout=45, extra_headers=headers, method=method, data=data)
        except http.FetchError as error:
            if error.status in (401, 403):
                raise SyncError("the sync server rejected the username or password")
            if error.status == 404:
                raise SyncError("the sync server has no such endpoint (%s)" % path)
            raise SyncError(error.message)
        if not response.body.strip():
            return {}
        try:
            return json.loads(response.body.decode("utf-8", "replace"))
        except ValueError:
            raise SyncError("the sync server answered with something that is not JSON")

    # ---- devices -----------------------------------------------------------

    def register_device(self, caption):
        if self.provider == "nextcloud":
            return
        self._call("POST", "/api/2/devices/%s/%s.json" % (self.username, self.device_id), body={"caption": caption, "type": "desktop"})

    # ---- subscriptions -----------------------------------------------------

    def push_subscriptions(self, add, remove):
        body = {"add": list(add), "remove": list(remove)}
        if self.provider == "nextcloud":
            return self._call("POST", "/subscription_change/create", body=body)
        return self._call("POST", "/api/2/subscriptions/%s/%s.json" % (self.username, self.device_id), body=body)

    def pull_subscriptions(self, since):
        if self.provider == "nextcloud":
            return self._call("GET", "/subscriptions", params={"since": int(since)})
        return self._call("GET", "/api/2/subscriptions/%s/%s.json" % (self.username, self.device_id), params={"since": int(since)})

    # ---- episode actions ---------------------------------------------------

    def push_actions(self, actions):
        if self.provider == "nextcloud":
            return self._call("POST", "/episode_action/create", body=actions)
        return self._call("POST", "/api/2/episodes/%s.json" % self.username, body=actions)

    def pull_actions(self, since):
        if self.provider == "nextcloud":
            return self._call("GET", "/episode_action", params={"since": int(since)})
        return self._call("GET", "/api/2/episodes/%s.json" % self.username, params={"since": int(since), "aggregated": "true"})


class Sync:
    def __init__(self, engine):
        self.engine = engine
        self.store = None
        self._running = False
        self._task = None
        self._debounce = None
        self._last_error = ""
        self._last_sync = 0

    async def start(self):
        self.store = self.engine.store
        self._last_sync = int(self.store.get_sync_state("last_sync_at", 0) or 0)
        self.engine.on_settings_changed(self._on_settings)
        self.engine.on_playback_settled = self.request_soon
        self._broadcast()
        if self.enabled:
            self.engine.scheduler.every("sync", max(60, int(self.engine.settings.syncIntervalMin) * 60), self.run, initial_delay=25)

    async def stop(self, restart=False, quit_mpv=True):
        if self._debounce is not None:
            self._debounce.cancel()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @property
    def enabled(self):
        provider = str(self.engine.settings.syncProvider or "none")
        return provider in ("gpodder", "nextcloud") and bool(self.engine.settings.syncUsername) and bool(self.engine.credential("sync", "password"))

    def _client(self):
        settings = self.engine.settings
        return Client(str(settings.syncProvider), str(settings.syncServer), str(settings.syncUsername),
                      self.engine.credential("sync", "password"), settings.sync_device_id)

    def _broadcast(self):
        if self.store is None or getattr(self.store, "conn", None) is None:
            return  # the store is already closed: shutting down
        try:
            pending = (self.store.scalar("SELECT COUNT(*) FROM episode_actions WHERE synced = 0", default=0)
                       + self.store.scalar("SELECT COUNT(*) FROM subscription_changes WHERE synced = 0", default=0))
        except sqlite3.Error:
            return
        self.engine.set_state("sync", {
            "provider": str(self.engine.settings.syncProvider or "none"),
            "configured": self.enabled,
            "lastSyncAt": self._last_sync,
            "lastError": self._last_error,
            "pending": pending,
            "syncing": self._running,
        })

    def _on_settings(self):
        scheduler = self.engine.scheduler
        interval = max(60, int(self.engine.settings.syncIntervalMin) * 60)
        existing = [job for job in scheduler.jobs if job.name == "sync"]
        if self.enabled and existing:
            # Keep the job object: a run in flight stays owned by the scheduler
            # so stop() can wait for it; only the cadence changes.
            for job in existing:
                job.interval = interval
                job.next_at = min(job.next_at, time.monotonic() + 5)
        elif self.enabled:
            scheduler.every("sync", interval, self.run, initial_delay=5)
        else:
            scheduler.jobs = [job for job in scheduler.jobs if job.name != "sync"]
        self._broadcast()

    def request_soon(self):
        """After a pause/stop/subscribe: sync shortly, coalescing bursts."""
        if not self.enabled:
            return
        if self._debounce is not None:
            self._debounce.cancel()
        self._debounce = self.engine.loop.call_later(DEBOUNCE_SECONDS, self._start_run)

    def _start_run(self):
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self.run())

    # ---- the cycle ---------------------------------------------------------

    async def run(self):
        if not self.enabled or self._running:
            return {"synced": False, "reason": "not configured" if not self.enabled else "already running"}
        self._running = True
        self._broadcast()
        client = self._client()
        try:
            if not self.store.get_sync_state("device_registered", False):
                await self.engine.run_in_thread(client.register_device, "Omarchy-Podcast (%s)" % client.device_id)
                self.store.set_sync_state("device_registered", True)
            await self._sync_subscriptions(client)
            await self._sync_actions(client)
            self._last_error = ""
            self._last_sync = now()
            self.store.set_sync_state("last_sync_at", self._last_sync)
            LOG.info("sync with %s done", client.provider)
            return {"synced": True, "at": self._last_sync}
        except asyncio.CancelledError:
            raise
        except SyncError as error:
            self._last_error = str(error)
            LOG.warning("sync failed: %s", error)
            self.engine.notice("warn", "Sync failed: %s" % error, code="sync")
            return {"synced": False, "error": str(error)}
        except Exception as error:  # noqa: BLE001
            self._last_error = "%s: %s" % (type(error).__name__, error)
            LOG.exception("sync crashed")
            return {"synced": False, "error": self._last_error}
        finally:
            self._running = False
            self._broadcast()

    async def _sync_subscriptions(self, client):
        library = self.engine.library
        pending = self.store.all("SELECT id, feed_url, action FROM subscription_changes WHERE synced = 0 ORDER BY timestamp, id")
        # Only the last word per feed counts: an add followed by a remove
        # pushed together is an error at gpodder.net.
        latest = {}
        for row in pending:
            latest[row["feed_url"]] = row["action"]
        add = [url for url, action in latest.items() if action == "add"]
        remove = [url for url, action in latest.items() if action == "remove"]
        if pending:
            result = await self.engine.run_in_thread(client.push_subscriptions, add, remove)
            self.store.execute("UPDATE subscription_changes SET synced = 1 WHERE id IN (%s)" % ",".join(str(r["id"]) for r in pending))
            for pair in (result or {}).get("update_urls", []) or []:
                if not (isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str) and isinstance(pair[1], str)):
                    continue
                if pair[0] == pair[1] or not pair[1].lower().startswith(("http://", "https://")):
                    continue
                try:
                    self.store.execute("UPDATE podcasts SET feed_url = ? WHERE feed_url = ?", (pair[1][:2000], pair[0]))
                except sqlite3.IntegrityError:
                    LOG.warning("sync: server rewrote %s to %s, which is already subscribed", pair[0], pair[1])
        since = int(self.store.get_sync_state("last_sub_ts", 0) or 0)
        # Changes recorded while the remote ones are applied below belong to
        # the user (unless they concern the very feeds the server sent).
        mark_from = int(self.store.scalar("SELECT COALESCE(MAX(id), 0) FROM subscription_changes", default=0) or 0)
        touched = set()
        remote = await self.engine.run_in_thread(client.pull_subscriptions, since)
        known = {canonical_feed_url(r["feed_url"]): r for r in self.store.all("SELECT id, feed_url, subscribed_at FROM podcasts")}
        local_removed = {canonical_feed_url(r["feed_url"]) for r in self.store.all(
            "SELECT feed_url FROM subscription_changes WHERE action = 'remove' AND timestamp >= ?", (since,))}
        for url in (remote or {}).get("add", []) or []:
            key = canonical_feed_url(url)
            if key in known or key in local_removed:
                continue
            touched.add(key)
            try:
                summary = await library.subscribe(url)
                # A redirect may have stored the feed under another URL.
                touched.add(canonical_feed_url(summary.get("feedUrl") or url))
                LOG.info("sync: subscribed to %s", url)
            except protocol.ProtocolError as error:
                LOG.warning("sync: could not subscribe to %s: %s", url, error.message)
        for url in (remote or {}).get("remove", []) or []:
            row = known.get(canonical_feed_url(url))
            if row is None:
                continue
            # A subscription made here after the remote removal stays.
            if int(row["subscribed_at"] or 0) > since:
                continue
            touched.add(canonical_feed_url(row["feed_url"]))
            library.unsubscribe(row["id"], False)
            LOG.info("sync: unsubscribed from %s", url)
        # Those came from the server; do not push them back.
        for row in self.store.all("SELECT id, feed_url FROM subscription_changes WHERE synced = 0 AND id > ?", (mark_from,)):
            if canonical_feed_url(row["feed_url"]) in touched:
                self.store.execute("UPDATE subscription_changes SET synced = 1 WHERE id = ?", (row["id"],))
        stamp = (remote or {}).get("timestamp")
        self.store.set_sync_state("last_sub_ts", int(stamp) if isinstance(stamp, (int, float)) else now())

    async def _sync_actions(self, client):
        rows = self.store.all("SELECT * FROM episode_actions WHERE synced = 0 ORDER BY timestamp LIMIT ?", (BATCH,))
        while rows:
            actions = []
            for row in rows:
                action = {
                    "podcast": row["podcast_feed_url"], "episode": row["episode_url"], "action": row["action"],
                    "timestamp": _iso(row["timestamp"]),
                }
                if client.provider == "gpodder":
                    action["device"] = client.device_id
                if client.provider == "nextcloud" and row["episode_guid"]:
                    action["guid"] = row["episode_guid"]
                if row["action"] == "play":
                    action["started"] = int(row["started"] or 0)
                    action["position"] = int(row["position"] or 0)
                    action["total"] = int(row["total"] or 0)
                actions.append(action)
            await self.engine.run_in_thread(client.push_actions, actions)
            self.store.execute("UPDATE episode_actions SET synced = 1 WHERE id IN (%s)" % ",".join(str(r["id"]) for r in rows))
            rows = self.store.all("SELECT * FROM episode_actions WHERE synced = 0 ORDER BY timestamp LIMIT ?", (BATCH,))

        since = int(self.store.get_sync_state("last_action_ts", 0) or 0)
        remote = await self.engine.run_in_thread(client.pull_actions, since)
        applied = 0
        for action in (remote or {}).get("actions", []) or []:
            if self._apply_action(action):
                applied += 1
        stamp = (remote or {}).get("timestamp")
        self.store.set_sync_state("last_action_ts", int(stamp) if isinstance(stamp, (int, float)) else now())
        if applied:
            self.engine.library.broadcast_inbox()
            self.engine.library.broadcast_library()
            LOG.info("sync: applied %d remote action(s)", applied)

    def _find_episode(self, action):
        guid = str(action.get("guid") or "")
        url = str(action.get("episode") or "")
        feed = canonical_feed_url(str(action.get("podcast") or ""))
        row = None
        if guid:
            # GUIDs are only unique within a feed.
            candidates = self.store.all("SELECT e.id, e.position, e.position_updated_at, e.duration, e.played, p.feed_url "
                                        "FROM episodes e JOIN podcasts p ON p.id = e.podcast_id WHERE e.guid = ?", (guid,))
            for candidate in candidates:
                if not feed or canonical_feed_url(candidate["feed_url"]) == feed:
                    row = candidate
                    break
        if row is None and url:
            row = self.store.one("SELECT id, position, position_updated_at, duration, played FROM episodes WHERE enclosure_url = ?", (url,))
            if row is None:
                key = canonical_feed_url(url)
                for candidate in self.store.all("SELECT id, position, position_updated_at, duration, played, enclosure_url FROM episodes WHERE enclosure_url LIKE ?",
                                                ("%" + url.rsplit("/", 1)[-1][:60],)):
                    if canonical_feed_url(candidate["enclosure_url"]) == key:
                        row = candidate
                        break
        return row

    def _apply_action(self, action):
        if not isinstance(action, dict):
            return False
        kind = str(action.get("action") or "").lower()
        stamp = _from_iso(action.get("timestamp")) or 0
        row = self._find_episode(action)
        if row is None:
            return False
        library = self.engine.library
        if kind == "play":
            position = action.get("position")
            total = action.get("total")
            try:
                position = float(position) if position is not None else None
                total = float(total) if total is not None and float(total) > 0 else None
            except (TypeError, ValueError):
                return False
            if position is None or position < 0:
                return False
            if int(row["position_updated_at"] or 0) >= stamp:
                return False
            duration = total or float(row["duration"] or 0)
            self.store.execute("UPDATE episodes SET position = ?, position_updated_at = ?, last_played_at = COALESCE(last_played_at, ?) WHERE id = ?",
                               (position, stamp, stamp, row["id"]))
            if duration > 0 and (position >= duration - PLAYED_MARGIN or position / duration >= 0.95) and not row["played"]:
                self.store.execute("UPDATE episodes SET played = 1, played_at = ?, state = 'archived' WHERE id = ?", (stamp, row["id"]))
                self.store.execute("DELETE FROM queue WHERE episode_id = ?", (row["id"],))
            playback = getattr(self.engine, "playback", None)
            if playback is not None and playback.current_id == row["id"] and not playback.loaded:
                playback.pos = position
                playback.broadcast_player(force=True)
            library.emit_episode(row["id"])
            return True
        if kind == "new":
            self.store.execute("UPDATE episodes SET state = 'inbox', played = 0 WHERE id = ?", (row["id"],))
            library.emit_episode(row["id"])
            return True
        return False


# ---------------------------------------------------------------- commands

@protocol.command("sync-now", "Sync subscriptions and positions now")
async def cmd_sync_now(engine, client):
    return await engine.sync.run()


@protocol.command("sync-status", "Sync state")
def cmd_sync_status(engine, client):
    engine.sync._broadcast()
    return engine.state["sync"]


@protocol.command("sync-reset", "Forget sync timestamps (next sync pulls everything again)")
def cmd_sync_reset(engine, client):
    for key in ("last_sub_ts", "last_action_ts", "device_registered"):
        engine.store.execute("DELETE FROM sync_state WHERE key = ?", (key,))
    engine.sync._broadcast()
    return {"reset": True}
