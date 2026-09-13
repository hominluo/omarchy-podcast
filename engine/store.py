"""SQLite storage: one connection, WAL, versioned migrations.

Every query in the engine goes through the event-loop thread. Worker threads
(feed fetches, downloads, whisper) hand parsed results back with
`Engine.call_soon` and never touch the connection, which is what lets one
connection with `check_same_thread` left on be enough.

Schema changes are appended to MIGRATIONS as `(version, [statements])`; the
current `PRAGMA user_version` says which have run. A fresh database runs all
of them inside one transaction per version.
"""

import contextlib
import json
import os
import sqlite3
import time

from . import log

LOG = log.get("store")

MIGRATIONS = [
    (1, [
        """
        CREATE TABLE podcasts (
          id INTEGER PRIMARY KEY,
          feed_url TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL DEFAULT '',
          author TEXT NOT NULL DEFAULT '',
          description_html TEXT NOT NULL DEFAULT '',
          description_text TEXT NOT NULL DEFAULT '',
          link TEXT NOT NULL DEFAULT '',
          language TEXT NOT NULL DEFAULT '',
          image_url TEXT NOT NULL DEFAULT '',
          artwork_path TEXT NOT NULL DEFAULT '',
          podcast_guid TEXT,
          itunes_id INTEGER,
          pi_id INTEGER,
          funding_json TEXT NOT NULL DEFAULT '[]',
          subscribed_at INTEGER NOT NULL,
          etag TEXT,
          last_modified TEXT,
          last_fetch_at INTEGER,
          last_fetch_ok INTEGER NOT NULL DEFAULT 1,
          last_error TEXT NOT NULL DEFAULT '',
          fail_count INTEGER NOT NULL DEFAULT 0,
          next_refresh_at INTEGER NOT NULL DEFAULT 0,
          speed REAL,
          auto_download TEXT,
          skip_intro_sec INTEGER,
          skip_outro_sec INTEGER,
          auto_queue TEXT NOT NULL DEFAULT 'none' CHECK (auto_queue IN ('none','next','last'))
        )
        """,
        "CREATE INDEX idx_podcasts_due ON podcasts(next_refresh_at)",
        """
        CREATE TABLE episodes (
          id INTEGER PRIMARY KEY,
          podcast_id INTEGER NOT NULL REFERENCES podcasts(id) ON DELETE CASCADE,
          guid TEXT NOT NULL,
          title TEXT NOT NULL DEFAULT '',
          link TEXT NOT NULL DEFAULT '',
          pub_date INTEGER,
          duration INTEGER,
          enclosure_url TEXT NOT NULL,
          enclosure_type TEXT NOT NULL DEFAULT '',
          enclosure_length INTEGER,
          notes_html TEXT NOT NULL DEFAULT '',
          notes_text TEXT NOT NULL DEFAULT '',
          image_url TEXT NOT NULL DEFAULT '',
          artwork_path TEXT NOT NULL DEFAULT '',
          episode_number INTEGER,
          season INTEGER,
          episode_type TEXT NOT NULL DEFAULT 'full',
          explicit INTEGER NOT NULL DEFAULT 0,
          chapters_url TEXT NOT NULL DEFAULT '',
          chapters_type TEXT NOT NULL DEFAULT '',
          transcripts_json TEXT NOT NULL DEFAULT '[]',
          persons_json TEXT NOT NULL DEFAULT '[]',
          content_hash TEXT NOT NULL DEFAULT '',
          state TEXT NOT NULL DEFAULT 'inbox' CHECK (state IN ('inbox','archived')),
          played INTEGER NOT NULL DEFAULT 0,
          played_at INTEGER,
          position REAL NOT NULL DEFAULT 0,
          position_updated_at INTEGER,
          last_played_at INTEGER,
          first_seen_at INTEGER NOT NULL,
          UNIQUE (podcast_id, guid)
        )
        """,
        "CREATE INDEX idx_episodes_podcast_pub ON episodes(podcast_id, pub_date DESC, id DESC)",
        "CREATE INDEX idx_episodes_inbox ON episodes(pub_date DESC) WHERE state = 'inbox' AND played = 0",
        "CREATE INDEX idx_episodes_history ON episodes(last_played_at DESC) WHERE last_played_at IS NOT NULL",
        "CREATE INDEX idx_episodes_enclosure ON episodes(enclosure_url)",
        """
        CREATE TABLE queue (
          episode_id INTEGER PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
          position INTEGER NOT NULL UNIQUE,
          added_at INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE downloads (
          episode_id INTEGER PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK (status IN ('queued','downloading','paused','done','error')),
          keep INTEGER NOT NULL DEFAULT 1,
          path TEXT NOT NULL DEFAULT '',
          temp_path TEXT NOT NULL DEFAULT '',
          bytes_done INTEGER NOT NULL DEFAULT 0,
          bytes_total INTEGER,
          etag TEXT,
          last_modified TEXT,
          attempts INTEGER NOT NULL DEFAULT 0,
          error TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL,
          finished_at INTEGER
        )
        """,
        "CREATE INDEX idx_downloads_status ON downloads(status, created_at)",
        """
        CREATE TABLE chapters_cache (
          episode_id INTEGER PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
          source TEXT NOT NULL CHECK (source IN ('pi-json','mpv','merged')),
          chapters_json TEXT NOT NULL,
          fetched_at INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE transcripts (
          episode_id INTEGER PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
          source TEXT NOT NULL CHECK (source IN ('feed','whisper')),
          source_url TEXT NOT NULL DEFAULT '',
          source_type TEXT NOT NULL DEFAULT '',
          language TEXT NOT NULL DEFAULT '',
          path TEXT NOT NULL,
          segment_count INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL CHECK (status IN ('queued','partial','complete','error')),
          progress_sec REAL NOT NULL DEFAULT 0,
          model TEXT NOT NULL DEFAULT '',
          error TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL
        )
        """,
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        """
        CREATE TABLE episode_actions (
          id INTEGER PRIMARY KEY,
          podcast_feed_url TEXT NOT NULL,
          episode_url TEXT NOT NULL,
          episode_guid TEXT NOT NULL DEFAULT '',
          action TEXT NOT NULL CHECK (action IN ('download','delete','play','new')),
          started REAL,
          position REAL,
          total REAL,
          timestamp INTEGER NOT NULL,
          synced INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX idx_actions_unsynced ON episode_actions(synced, timestamp)",
        """
        CREATE TABLE subscription_changes (
          id INTEGER PRIMARY KEY,
          feed_url TEXT NOT NULL,
          action TEXT NOT NULL CHECK (action IN ('add','remove')),
          timestamp INTEGER NOT NULL,
          synced INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        """
        CREATE TABLE search_cache (
          key TEXT PRIMARY KEY,
          response_json TEXT NOT NULL,
          fetched_at INTEGER NOT NULL
        )
        """,
    ]),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


class Store:
    def __init__(self, path):
        self.path = path
        self.conn = None

    def open(self):
        first = self.path == ":memory:" or not _exists(self.path)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.migrate()
        if not first:
            check = self.conn.execute("PRAGMA quick_check").fetchone()
            if check and check[0] != "ok":
                LOG.error("database quick_check: %s", check[0])
        return self

    def close(self):
        if self.conn is None:
            return
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self.conn.close()
        self.conn = None

    def migrate(self):
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for version, statements in MIGRATIONS:
            if version <= current:
                continue
            LOG.info("migrating database to schema %d", version)
            with self.transaction():
                for statement in statements:
                    self.conn.execute(statement)
                self.conn.execute("PRAGMA user_version = %d" % version)

    @contextlib.contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ---- thin helpers ------------------------------------------------------

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql, params=(), default=None):
        row = self.one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    # ---- key/value tables --------------------------------------------------

    def get_setting(self, key, default=None):
        raw = self.scalar("SELECT value FROM settings WHERE key = ?", (key,))
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def set_setting(self, key, value):
        self.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)))

    def get_sync_state(self, key, default=None):
        raw = self.scalar("SELECT value FROM sync_state WHERE key = ?", (key,))
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def set_sync_state(self, key, value):
        self.execute(
            "INSERT INTO sync_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)))

    def counts(self):
        return {
            "podcasts": self.scalar("SELECT COUNT(*) FROM podcasts", default=0),
            "episodes": self.scalar("SELECT COUNT(*) FROM episodes", default=0),
            "queue": self.scalar("SELECT COUNT(*) FROM queue", default=0),
            "inbox": self.scalar("SELECT COUNT(*) FROM episodes WHERE state = 'inbox' AND played = 0", default=0),
            "downloads": self.scalar("SELECT COUNT(*) FROM downloads WHERE status = 'done'", default=0),
            "transcripts": self.scalar("SELECT COUNT(*) FROM transcripts WHERE status = 'complete'", default=0),
        }


def _exists(path):
    try:
        return os.path.exists(path)
    except OSError:
        return False


def now():
    return int(time.time())


def row_dict(row):
    return {key: row[key] for key in row.keys()} if row is not None else None
