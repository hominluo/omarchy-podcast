"""Up Next: an ordered list of episodes waiting to play.

Positions step by 1000 so an insert between two rows rarely needs a
renumber. The whole list is small (it is a listening queue, not a library),
so every change rebroadcasts it in full.
"""

from . import log, models, protocol
from .store import now

LOG = log.get("queue")
A = protocol.Arg
STEP = 1000


class Queue:
    def __init__(self, engine):
        self.engine = engine
        self.store = None

    async def start(self):
        self.store = self.engine.store
        self.broadcast()

    async def stop(self, restart=False, quit_mpv=True):
        pass

    # ---- reads -------------------------------------------------------------

    def rows(self):
        return self.store.all(models.EPISODE_SELECT + " WHERE q.episode_id IS NOT NULL ORDER BY q.position ASC")

    def items(self):
        return [models.episode_summary(row) for row in self.rows()]

    def ids(self):
        return [row["episode_id"] for row in self.store.all("SELECT episode_id FROM queue ORDER BY position ASC")]

    def head(self):
        row = self.store.one("SELECT episode_id FROM queue ORDER BY position ASC LIMIT 1")
        return row["episode_id"] if row else None

    def contains(self, episode_id):
        return self.store.one("SELECT 1 FROM queue WHERE episode_id = ?", (int(episode_id),)) is not None

    def broadcast(self):
        self.engine.set_state("queue", self.items())

    # ---- writes ------------------------------------------------------------

    def add(self, episode_ids, where="last"):
        stamp = now()
        added = []
        with self.store.transaction():
            for episode_id in episode_ids:
                episode_id = int(episode_id)
                if self.store.one("SELECT id FROM episodes WHERE id = ?", (episode_id,)) is None:
                    continue
                if self.contains(episode_id):
                    if where == "next":
                        self._move_locked(episode_id, 0)
                    continue
                if where == "next":
                    self._shift_all(STEP)
                    position = STEP // 2
                else:
                    position = int(self.store.scalar("SELECT COALESCE(MAX(position), 0) FROM queue", default=0)) + STEP
                self.store.execute("INSERT INTO queue (episode_id, position, added_at) VALUES (?, ?, ?)", (episode_id, position, stamp))
                added.append(episode_id)
            self._renumber_if_needed()
        self.broadcast()
        for episode_id in added:
            self.engine.library.emit_episode(episode_id)
        return {"added": added}

    def remove(self, episode_ids):
        removed = []
        with self.store.transaction():
            for episode_id in episode_ids:
                cursor = self.store.execute("DELETE FROM queue WHERE episode_id = ?", (int(episode_id),))
                if cursor.rowcount:
                    removed.append(int(episode_id))
        self.broadcast()
        for episode_id in removed:
            self.engine.library.emit_episode(episode_id)
        return {"removed": removed}

    def move(self, episode_id, index):
        with self.store.transaction():
            self._move_locked(int(episode_id), int(index))
        self.broadcast()
        return {"queue": self.ids()}

    def _move_locked(self, episode_id, index):
        ids = self.ids()
        if episode_id not in ids:
            raise protocol.ProtocolError(protocol.NOT_FOUND, "episode %d is not in Up Next" % episode_id)
        ids.remove(episode_id)
        index = max(0, min(len(ids), index))
        ids.insert(index, episode_id)
        for position, eid in enumerate(ids):
            self.store.execute("UPDATE queue SET position = ? WHERE episode_id = ?", (-(position + 1), eid))
        for position, eid in enumerate(ids):
            self.store.execute("UPDATE queue SET position = ? WHERE episode_id = ?", ((position + 1) * STEP, eid))

    def clear(self):
        ids = self.ids()
        self.store.execute("DELETE FROM queue")
        self.broadcast()
        for episode_id in ids:
            self.engine.library.emit_episode(episode_id)
        return {"removed": ids}

    def pop_head(self):
        episode_id = self.head()
        if episode_id is not None:
            self.store.execute("DELETE FROM queue WHERE episode_id = ?", (episode_id,))
            self.broadcast()
            self.engine.library.emit_episode(episode_id)
        return episode_id

    def _shift_all(self, delta):
        # Two passes keep the UNIQUE(position) constraint happy mid-update.
        self.store.execute("UPDATE queue SET position = -(position + ?)", (delta,))
        self.store.execute("UPDATE queue SET position = -position")

    def _renumber_if_needed(self):
        smallest_gap = self.store.scalar(
            "SELECT MIN(b.position - a.position) FROM queue a JOIN queue b ON b.position > a.position", default=STEP)
        if smallest_gap is not None and smallest_gap < 2:
            ids = self.ids()
            for position, eid in enumerate(ids):
                self.store.execute("UPDATE queue SET position = ? WHERE episode_id = ?", (-(position + 1), eid))
            for position, eid in enumerate(ids):
                self.store.execute("UPDATE queue SET position = ? WHERE episode_id = ?", ((position + 1) * STEP, eid))


# ---------------------------------------------------------------- commands

@protocol.command("queue-add", "Add episodes to Up Next", episodeIds=A("int-list"),
                  where=A(str, required=False, default="last", choices=["next", "last"]))
def cmd_queue_add(engine, client, episodeIds, where):
    return engine.queue.add(episodeIds, where)


@protocol.command("queue-remove", "Remove episodes from Up Next", episodeIds=A("int-list"))
def cmd_queue_remove(engine, client, episodeIds):
    return engine.queue.remove(episodeIds)


@protocol.command("queue-move", "Move an episode to an index in Up Next", episodeId=A(int), index=A(int, minimum=0))
def cmd_queue_move(engine, client, episodeId, index):
    return engine.queue.move(episodeId, index)


@protocol.command("queue-clear", "Empty Up Next")
def cmd_queue_clear(engine, client):
    return engine.queue.clear()
