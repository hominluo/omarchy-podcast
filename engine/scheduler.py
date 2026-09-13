"""Periodic work: feed refreshes when they fall due, artwork sweeps, daily
housekeeping, and whatever later subsystems register.

One tick a minute. Each job is a coroutine or a plain callable registered
with `every(seconds, fn)`; a job that raises is logged and tried again on
its next turn, never allowed to stop the others.
"""

import asyncio
import subprocess
import time

from . import log

LOG = log.get("scheduler")

TICK = 60
FIRST_TICK = 15


class Job:
    def __init__(self, name, interval, fn, initial_delay):
        self.name = name
        self.interval = interval
        self.fn = fn
        self.next_at = time.monotonic() + initial_delay
        self.running = False
        self.task = None


class Scheduler:
    def __init__(self, engine):
        self.engine = engine
        self.jobs = []
        self._task = None

    def every(self, name, interval, fn, initial_delay=None):
        delay = FIRST_TICK if initial_delay is None else initial_delay
        self.jobs.append(Job(name, interval, fn, delay))

    async def start(self):
        self.every("refresh-due-feeds", TICK, self._refresh_due)
        self.every("artwork-sweep", 5 * 60, self._artwork_sweep, initial_delay=20)
        self.every("housekeeping", 24 * 3600, self._housekeeping, initial_delay=120)
        self._task = asyncio.ensure_future(self._run())

    async def stop(self, restart=False, quit_mpv=True):
        tasks = [self._task] + [job.task for job in self.jobs]
        for task in tasks:
            if task and not task.done():
                task.cancel()
        for task in tasks:
            if task and not task.done():
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _run(self):
        while True:
            await asyncio.sleep(1)
            stamp = time.monotonic()
            for job in self.jobs:
                if job.running or stamp < job.next_at:
                    continue
                job.next_at = stamp + job.interval
                job.running = True
                job.task = asyncio.ensure_future(self._run_job(job))

    async def _run_job(self, job):
        try:
            result = job.fn()
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            LOG.exception("job %s failed", job.name)
        finally:
            job.running = False
            job.task = None

    def kick(self, name):
        """Run a job on the next tick regardless of its schedule."""
        for job in self.jobs:
            if job.name == name:
                job.next_at = 0

    # ---- built-in jobs -----------------------------------------------------

    async def _refresh_due(self):
        library = getattr(self.engine, "library", None)
        if library is None:
            return
        result = await library.refresh(None, force=False)
        if result.get("refreshed"):
            LOG.info("refreshed %d due feed(s), %d new episode(s)", result["refreshed"], result.get("added", 0))
            if result.get("added") and self.engine.settings.notifyNewEpisodes:
                self._notify_new(result["added"])
            artwork = getattr(self.engine, "artwork", None)
            if artwork is not None:
                artwork.sweep()

    def _notify_new(self, count):
        text = "%d new episode%s" % (count, "" if count == 1 else "s")
        try:
            subprocess.Popen(["omarchy-notification-send", "--app-name", "Podcast", "-u", "low", "Podcast", text],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def _artwork_sweep(self):
        artwork = getattr(self.engine, "artwork", None)
        if artwork is not None:
            artwork.sweep()

    async def _housekeeping(self):
        store = self.engine.store
        search = getattr(self.engine, "search", None)
        if search is not None:
            search.prune_cache()
        artwork = getattr(self.engine, "artwork", None)
        if artwork is not None:
            removed = artwork.prune()
            if removed:
                LOG.info("pruned %d unreferenced artwork file(s)", removed)
        # Synced action rows are only history; keep three months.
        store.execute("DELETE FROM episode_actions WHERE synced = 1 AND timestamp < ?", (int(time.time()) - 90 * 86400,))
        store.execute("DELETE FROM subscription_changes WHERE synced = 1 AND timestamp < ?", (int(time.time()) - 90 * 86400,))
        for hook in getattr(self.engine, "housekeeping_hooks", []):
            try:
                result = hook()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                LOG.exception("housekeeping hook failed")
