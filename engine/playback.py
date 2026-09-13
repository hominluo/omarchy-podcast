"""Playback policy on top of the mpv client.

Everything that makes mpv behave like a podcast player lives here: loading
an episode at its saved position, remembering where it got to, marking it
played near the end, moving on to Up Next, per-podcast speed, the sleep
timer, and the two audio toggles (silence skipping through the bundled Lua
script, voice boost through an mpv audio filter).

The `player` state slice is derived from mpv's observed properties and
broadcast at most a few times a second; `nowPlaying` carries the episode,
its podcast and its chapters and changes only when the track does.
"""

import asyncio
import os
import time

from . import log, models, protocol
from .player import MpvError
from .store import now

LOG = log.get("playback")
A = protocol.Arg

BROADCAST_INTERVAL = 0.25
FLUSH_INTERVAL = 5.0
PLAYED_THRESHOLD = 0.95
RESUME_MARGIN = 5.0
FADE_STEPS = 10
FADE_SECONDS = 3.0
VOICE_FILTER = "@voice:dynaudnorm=f=150:g=15:p=0.7:m=10"
STALL_SECONDS = 30.0
SPEED_MIN, SPEED_MAX = 0.5, 3.0


class Playback:
    def __init__(self, engine):
        self.engine = engine
        self.mpv = engine.mpv
        self.store = None
        self.current_id = None        # episode shown as "now playing"
        self.current_row = None
        self.loaded = False           # mpv actually has current_id loaded
        self.loading_id = None        # loadfile in flight for this id
        self.pos = 0.0
        self.duration = 0.0
        self.sleep = {"mode": "off", "endsAt": 0}
        self._sleep_handle = None
        self._fading = False
        self._broadcast_handle = None
        self._broadcast_dirty = False
        self._last_broadcast = 0.0
        self._last_flush_pos = None
        self._last_flush_at = 0.0
        self._finished_for = None
        self._retries = 0
        self._stall_handle = None
        self._expect_stop = False
        self._base_speed = 1.0
        self._voice_boost = False
        self._skip_silence = False

    # ---- lifecycle ---------------------------------------------------------

    async def start(self):
        self.store = self.engine.store
        self.mpv.on_property = self._on_property
        self.mpv.on_event = self._on_event
        self.mpv.on_connect = self._on_connect
        self.mpv.on_disconnect = self._on_disconnect
        persisted = self.engine.state["player"]
        self._base_speed = _clamp_speed(persisted.get("speed", 1.0))
        self._skip_silence = bool(persisted.get("skipSilence"))
        self._voice_boost = bool(persisted.get("voiceBoost"))
        self.engine.library.on_new_episodes = self._on_new_episodes
        # Show the last episode without touching the network: it loads when
        # the user presses play.
        last_id = self.store.get_setting("lastEpisodeId")
        if last_id is not None:
            row = self.engine.library.episode_row(int(last_id))
            if row is not None and not row["played"]:
                self.current_id = row["id"]
                self.current_row = row
                self.pos = float(row["position"] or 0)
                self.duration = float(row["duration"] or 0)
        self.broadcast_now_playing()
        self.broadcast_player(force=True)

    async def stop(self, restart=False, quit_mpv=True):
        self.flush_position(force=True, action=True)
        self._cancel_sleep(broadcast=False)
        if self._broadcast_handle:
            self._broadcast_handle.cancel()

    # ---- mpv callbacks -----------------------------------------------------

    async def _on_connect(self):
        """Fresh or inherited mpv. An inherited one (engine restart) tells us
        which episode it holds through user-data/podcast."""
        adopted = None
        try:
            adopted = await self.mpv.get_property("user-data/podcast")
        except MpvError:
            adopted = None
        if isinstance(adopted, dict) and adopted.get("episodeId") is not None:
            row = self.engine.library.episode_row(int(adopted["episodeId"]))
            if row is not None:
                self.current_id = row["id"]
                self.current_row = row
                self.loaded = True
                self.loading_id = None
                LOG.info("adopted running playback of episode %d", row["id"])
        try:
            await self.mpv.set_property("volume", int(self.engine.state["player"].get("volume", 100)))
        except MpvError:
            pass
        await self._apply_audio_toggles()
        if self.loaded:
            await self._apply_speed()
        self.broadcast_now_playing()
        self.broadcast_player(force=True)

    def _on_disconnect(self):
        self.flush_position(force=True, action=True)
        self.loaded = False
        self.loading_id = None
        self.broadcast_player(force=True)

    def _on_property(self, name, value):
        if name == "time-pos":
            if value is not None and self.loaded:
                self.pos = float(value)
                self._maybe_flush()
            self._schedule_broadcast()
        elif name == "duration":
            if value:
                self.duration = float(value)
                self._store_duration()
            self._schedule_broadcast()
        elif name == "eof-reached":
            if value is True and self.loaded:
                self._finished()
        elif name == "chapter-list":
            self.broadcast_now_playing()
        elif name == "chapter":
            self._schedule_broadcast()
            if self.sleep["mode"] == "chapter" and self.loaded and value not in (None, self.sleep.get("chapter")):
                self._sleep_fire()
        elif name == "paused-for-cache":
            self._schedule_broadcast()
            self._watch_stall(bool(value))
        elif name == "pause":
            if self.loaded:
                self.flush_position(force=True, action=bool(value))
            self._schedule_broadcast()
        elif name in ("speed", "volume", "mute", "idle-active", "core-idle", "seekable",
                      "user-data/skipsilence/enabled", "user-data/skipsilence/base_speed", "af", "cache-buffering-state"):
            if name == "volume" and value is not None and not self._fading:
                self.store.set_setting("volume", int(value))
            if name == "user-data/skipsilence/base_speed" and value:
                self._base_speed = _clamp_speed(value)
            self._schedule_broadcast()

    def _on_event(self, message):
        event = message.get("event")
        if event == "file-loaded":
            loaded_id = self.loading_id if self.loading_id is not None else self.current_id
            self.loading_id = None
            if loaded_id is not None and loaded_id == self.current_id:
                self.loaded = True
                self._retries = 0
                self._finished_for = None
                self.mpv.command_nowait("set_property", "user-data/podcast", {"episodeId": self.current_id})
                self.engine.loop.create_task(self._apply_speed())
                self.store.execute("UPDATE episodes SET last_played_at = ? WHERE id = ?", (now(), self.current_id))
                self.engine.library.emit_episode(self.current_id)
            self.broadcast_now_playing()
            self.broadcast_player(force=True)
        elif event == "end-file":
            reason = message.get("reason")
            if reason == "error":
                self._on_error(message.get("file_error") or "playback error")
            elif reason == "eof":
                if self.loaded:
                    self._finished()
            elif reason == "stop" and not self._expect_stop:
                self.loaded = False
                self.broadcast_player(force=True)
            self._expect_stop = False
        elif event in ("seek", "playback-restart"):
            self._schedule_broadcast()

    # ---- state broadcasting ------------------------------------------------

    def player_state(self):
        props = self.mpv.props
        paused = True if not self.loaded else bool(props.get("pause", True))
        skip = bool(props.get("user-data/skipsilence/enabled")) if self.loaded else self._skip_silence
        af = props.get("af") or []
        voice = any(isinstance(f, dict) and f.get("label") == "voice" for f in af) if self.loaded else self._voice_boost
        speed = float(props.get("speed") or self._base_speed) if self.loaded else self._base_speed
        return {
            "episodeId": self.current_id,
            "pos": round(self.pos, 2),
            "duration": round(self.duration, 2),
            "paused": paused,
            "idle": not self.loaded,
            "buffering": bool(props.get("paused-for-cache")) if self.loaded else False,
            "speed": round(speed, 3),
            "baseSpeed": round(self._base_speed, 3),
            "volume": int(props.get("volume") if props.get("volume") is not None else self.engine.state["player"].get("volume", 100)),
            "mute": bool(props.get("mute")),
            "chapter": int(props.get("chapter")) if self.loaded and props.get("chapter") is not None else -1,
            "skipSilence": skip,
            "voiceBoost": voice,
            "sleep": {"mode": self.sleep["mode"], "endsAt": self.sleep["endsAt"]},
            "mpv": self.mpv.state,
        }

    def _schedule_broadcast(self):
        self._broadcast_dirty = True
        if self._broadcast_handle is not None:
            return
        wait = max(0.0, BROADCAST_INTERVAL - (time.monotonic() - self._last_broadcast))
        self._broadcast_handle = self.engine.loop.call_later(wait, self._broadcast_now)

    def _broadcast_now(self):
        self._broadcast_handle = None
        if self._broadcast_dirty:
            self.broadcast_player(force=True)

    def broadcast_player(self, force=False):
        self._broadcast_dirty = False
        self._last_broadcast = time.monotonic()
        self.engine.set_state("player", self.player_state())

    def chapters(self):
        raw = self.mpv.props.get("chapter-list") if self.loaded else None
        chapters = []
        if isinstance(raw, list):
            for index, item in enumerate(raw):
                if not isinstance(item, dict):
                    continue
                chapters.append({"index": index, "startTime": float(item.get("time") or 0), "title": str(item.get("title") or "")})
        for index, chapter in enumerate(chapters):
            nxt = chapters[index + 1]["startTime"] if index + 1 < len(chapters) else (self.duration or None)
            chapter["endTime"] = nxt
        extra = getattr(self.engine, "chapters", None)
        if extra is not None and self.current_id is not None:
            try:
                chapters = extra.merge(self.current_id, chapters, self.duration)
            except Exception:  # noqa: BLE001
                LOG.exception("chapter merge failed")
        return chapters

    def broadcast_now_playing(self):
        if self.current_id is None or self.current_row is None:
            self.engine.set_state("nowPlaying", None)
            return
        row = self.engine.library.episode_row(self.current_id) or self.current_row
        self.current_row = row
        podcast = self.engine.library.podcast_row(row["podcast_id"])
        self.engine.set_state("nowPlaying", {
            "episode": models.episode_summary(row),
            "podcast": models.podcast_summary(podcast) if podcast is not None else None,
            "chapters": self.chapters(),
        })

    # ---- positions ---------------------------------------------------------

    def _maybe_flush(self):
        stamp = time.monotonic()
        if stamp - self._last_flush_at < FLUSH_INTERVAL:
            return
        if self._last_flush_pos is not None and abs(self.pos - self._last_flush_pos) < 1.0:
            return
        self.flush_position()

    def flush_position(self, force=False, action=False):
        if self.current_id is None:
            return
        if not self.loaded and not force:
            return
        self._last_flush_at = time.monotonic()
        self._last_flush_pos = self.pos
        row = self.store.one("SELECT played, duration FROM episodes WHERE id = ?", (self.current_id,))
        if row is None:
            return
        self.engine.library.set_position(self.current_id, self.pos, flush_action=action)
        duration = self.duration or float(row["duration"] or 0)
        if duration > 0 and not row["played"] and self.pos / duration >= PLAYED_THRESHOLD:
            self.store.execute("UPDATE episodes SET played = 1, played_at = ?, state = 'archived' WHERE id = ?", (now(), self.current_id))
            self.engine.library.emit_episode(self.current_id)
            self.engine.library.broadcast_inbox()
            self.engine.library.broadcast_library()

    def _store_duration(self):
        if self.current_id is None or not self.loaded or self.duration <= 0:
            return
        self.store.execute("UPDATE episodes SET duration = ? WHERE id = ? AND (duration IS NULL OR duration = 0 OR ABS(duration - ?) > 30)",
                           (int(self.duration), self.current_id, int(self.duration)))

    # ---- playing -----------------------------------------------------------

    def _source_for(self, row):
        if models.download_state(row) == "done":
            path = row["download_path"]
            if path and os.path.exists(path):
                return path, True
        url = row["enclosure_url"]
        if not url.lower().startswith(("http://", "https://")):
            raise protocol.ProtocolError(protocol.BAD_REQUEST, "this episode has no playable audio link")
        return url, False

    def _resume_position(self, row):
        position = float(row["position"] or 0)
        duration = float(row["duration"] or 0)
        if row["played"]:
            return 0.0
        if position > RESUME_MARGIN and (duration <= 0 or position < duration - RESUME_MARGIN):
            return position
        podcast = self.store.one("SELECT skip_intro_sec FROM podcasts WHERE id = ?", (row["podcast_id"],))
        intro = int(podcast["skip_intro_sec"] or 0) if podcast is not None else 0
        return float(intro) if intro > 0 and position <= RESUME_MARGIN else 0.0

    def effective_speed(self, row=None):
        row = row if row is not None else self.current_row
        if row is not None:
            podcast = self.store.one("SELECT speed FROM podcasts WHERE id = ?", (row["podcast_id"],))
            if podcast is not None and podcast["speed"]:
                return _clamp_speed(podcast["speed"])
        return _clamp_speed(self._base_speed)

    async def play(self, episode_id):
        row = self.engine.library.require_episode(episode_id)
        if self.current_id == row["id"] and self.loaded and self.loading_id is None:
            await self._set_pause(False)
            return self.player_state()
        if self.current_id is not None and self.loaded and self.current_id != row["id"]:
            self.flush_position(force=True, action=True)
        source, local = self._source_for(row)
        start = self._resume_position(row)
        if row["played"]:
            self.store.execute("UPDATE episodes SET played = 0, played_at = NULL, position = 0 WHERE id = ?", (row["id"],))
        self.current_id = row["id"]
        self.current_row = row
        self.loaded = False
        self.loading_id = row["id"]
        self.pos = start
        self.duration = float(row["duration"] or 0)
        self._finished_for = None
        self._last_flush_pos = None
        self.store.set_setting("lastEpisodeId", row["id"])
        self.engine.queue.remove([row["id"]])
        self.broadcast_now_playing()
        self.broadcast_player(force=True)
        if self.mpv.state != "connected":
            raise protocol.ProtocolError(protocol.UNAVAILABLE, "mpv is not running yet; try again in a moment")
        options = {"start": "%.3f" % start, "force-media-title": _media_title(row)}
        artwork = row["artwork_path"] or row["podcast_artwork_path"]
        if artwork and os.path.exists(artwork):
            options["cover-art-files"] = artwork
        self._expect_stop = True
        try:
            await self.mpv.command("loadfile", source, "replace", -1, options)
            await self.mpv.set_property("pause", False)
        except MpvError as error:
            self.loading_id = None
            raise protocol.ProtocolError(protocol.UNAVAILABLE, "mpv refused to play: %s" % error)
        LOG.info("playing episode %d (%s) from %.0fs %s", row["id"], row["title"][:60], start, "local" if local else "stream")
        self.engine.library.record_action(row["id"], "play", position=start, total=row["duration"], started=start)
        return self.player_state()

    async def _set_pause(self, paused):
        if not self.loaded:
            if not paused and self.current_id is not None:
                await self.play(self.current_id)
            return
        await self.mpv.set_property("pause", bool(paused))

    async def toggle(self):
        if not self.loaded:
            if self.current_id is not None:
                await self.play(self.current_id)
            elif self.engine.queue.head() is not None:
                await self.play(self.engine.queue.head())
            else:
                raise protocol.ProtocolError(protocol.NOT_FOUND, "nothing to play")
            return self.player_state()
        await self.mpv.set_property("pause", not bool(self.mpv.props.get("pause")))
        return self.player_state()

    async def stop_playback(self):
        self.flush_position(force=True, action=True)
        if self.loaded:
            self._expect_stop = True
            try:
                await self.mpv.command("stop")
            except MpvError:
                pass
        self.loaded = False
        self.loading_id = None
        self.broadcast_player(force=True)
        return self.player_state()

    async def seek(self, position, mode="absolute"):
        if not self.loaded:
            if self.current_id is None:
                raise protocol.ProtocolError(protocol.NOT_FOUND, "nothing is playing")
            target = position if mode == "absolute" else self.pos + position
            self.pos = max(0.0, min(target, self.duration) if self.duration > 0 else max(0.0, target))
            self.engine.library.set_position(self.current_id, self.pos)
            self.broadcast_player(force=True)
            return self.player_state()
        if mode == "absolute":
            await self.mpv.command("seek", float(position), "absolute+exact")
        else:
            await self.mpv.command("seek", float(position), "relative")
        return self.player_state()

    async def skip(self, direction):
        seconds = int(self.engine.settings.skipBack) if direction == "back" else int(self.engine.settings.skipForward)
        return await self.seek(-seconds if direction == "back" else seconds, "relative")

    async def next_episode(self):
        head = self.engine.queue.head()
        if head is None:
            raise protocol.ProtocolError(protocol.NOT_FOUND, "Up Next is empty")
        return await self.play(head)

    async def previous(self):
        if self.loaded and self.pos > 3:
            return await self.seek(0, "absolute")
        return await self.seek(0, "absolute") if self.current_id is not None else self.player_state()

    async def set_chapter(self, index):
        if not self.loaded:
            raise protocol.ProtocolError(protocol.NOT_FOUND, "nothing is playing")
        chapters = self.chapters()
        if index < 0 or index >= len(chapters):
            raise protocol.ProtocolError(protocol.NOT_FOUND, "no chapter %d" % index)
        return await self.seek(chapters[index]["startTime"], "absolute")

    # ---- finishing and advancing -------------------------------------------

    def _finished(self):
        if self.current_id is None or self._finished_for == self.current_id:
            return
        self._finished_for = self.current_id
        finished_id = self.current_id
        LOG.info("episode %d finished", finished_id)
        self.pos = self.duration or self.pos
        self.flush_position(force=True, action=True)
        self.engine.library.mark_played([finished_id], True)
        if self.sleep["mode"] == "episode":
            self._sleep_fire()
            return
        if not self.engine.settings.continuousPlayback:
            return
        head = self.engine.queue.head()
        if head is None:
            self.broadcast_player(force=True)
            return
        self.engine.loop.create_task(self._advance(head))

    async def _advance(self, episode_id):
        try:
            await self.play(episode_id)
        except protocol.ProtocolError as error:
            self.engine.notice("warn", "Could not play the next episode: %s" % error.message)

    def _on_error(self, detail):
        if self.current_id is None:
            return
        row = self.current_row
        LOG.warning("playback error on episode %s: %s", self.current_id, detail)
        self.loaded = False
        self.loading_id = None
        if self._retries == 0:
            self._retries = 1
            self.engine.loop.call_later(2.0, lambda: self.engine.loop.create_task(self._retry(row)))
            return
        self._retries = 0
        self.engine.notice("error", "Could not play “%s”: %s" % (row["title"][:60] if row else "episode", detail), episode_id=self.current_id)
        self.broadcast_player(force=True)

    async def _retry(self, row):
        if row is None or self.current_id != row["id"]:
            return
        # A broken download falls back to the stream.
        if models.download_state(row) == "done":
            self.store.execute("UPDATE downloads SET status = 'error', error = 'file would not play' WHERE episode_id = ?", (row["id"],))
        try:
            await self.play(row["id"])
        except protocol.ProtocolError as error:
            self.engine.notice("error", error.message, episode_id=row["id"])

    def _watch_stall(self, stalled):
        if self._stall_handle is not None:
            self._stall_handle.cancel()
            self._stall_handle = None
        if stalled and self.loaded:
            self._stall_handle = self.engine.loop.call_later(STALL_SECONDS, self._stall_fire)

    def _stall_fire(self):
        self._stall_handle = None
        if not self.loaded or not self.mpv.props.get("paused-for-cache"):
            return
        LOG.warning("stream stalled for %ds; reloading", STALL_SECONDS)
        self.engine.notice("warn", "The stream stalled; reconnecting…")
        self.engine.loop.create_task(self._retry(self.current_row))

    # ---- speed and audio toggles -------------------------------------------

    async def _apply_speed(self):
        if not self.loaded:
            return
        speed = self.effective_speed()
        if self._skip_silence and self.mpv.props.get("user-data/skipsilence/enabled") is not None:
            self.mpv.command_nowait("script-message-to", "skipsilence", "set-speed", "%.3f" % speed)
        else:
            try:
                await self.mpv.set_property("speed", speed)
            except MpvError:
                pass

    async def set_speed(self, speed, podcast_id=None):
        speed = _clamp_speed(speed)
        if podcast_id is not None:
            self.engine.library.update_podcast(podcast_id, speed=speed)
        else:
            self._base_speed = speed
            self.store.set_setting("speed", speed)
        await self._apply_speed()
        self.broadcast_player(force=True)
        return self.player_state()

    async def set_volume(self, volume):
        volume = max(0, min(100, int(volume)))
        self.store.set_setting("volume", volume)
        if self.mpv.state == "connected":
            await self.mpv.set_property("volume", volume)
        self.broadcast_player(force=True)
        return self.player_state()

    async def set_mute(self, mute):
        if self.mpv.state == "connected":
            await self.mpv.set_property("mute", bool(mute))
        return self.player_state()

    async def _apply_audio_toggles(self):
        if self.mpv.state != "connected":
            return
        if self.mpv.props.get("user-data/skipsilence/enabled") is None:
            try:
                enabled = await self.mpv.get_property("user-data/skipsilence/enabled")
                self.mpv.props["user-data/skipsilence/enabled"] = enabled
            except MpvError:
                pass
        self.mpv.command_nowait("script-message-to", "skipsilence", "enable" if self._skip_silence else "disable")
        af = await self._current_filters()
        has_voice = any(f.get("label") == "voice" for f in af)
        if self._voice_boost and not has_voice:
            self.mpv.command_nowait("af", "add", VOICE_FILTER)
        elif not self._voice_boost and has_voice:
            self.mpv.command_nowait("af", "remove", "@voice")

    async def _current_filters(self):
        try:
            af = await self.mpv.get_property("af")
        except MpvError:
            af = self.mpv.props.get("af")
        return [f for f in (af or []) if isinstance(f, dict)]

    async def set_skip_silence(self, enabled):
        self._skip_silence = bool(enabled)
        self.store.set_setting("skipSilence", self._skip_silence)
        if self.mpv.state == "connected":
            if self.mpv.props.get("user-data/skipsilence/enabled") is None and self._skip_silence:
                self.engine.notice("warn", "The silence-skipping script is not loaded in mpv", code="skipsilence-missing")
            self.mpv.command_nowait("script-message-to", "skipsilence", "enable" if enabled else "disable")
            await self._apply_speed()
        self.broadcast_player(force=True)
        return self.player_state()

    async def set_voice_boost(self, enabled):
        self._voice_boost = bool(enabled)
        self.store.set_setting("voiceBoost", self._voice_boost)
        await self._apply_audio_toggles()
        self._schedule_broadcast()
        return self.player_state()

    # ---- sleep timer -------------------------------------------------------

    def set_sleep(self, mode, minutes=None):
        self._cancel_sleep(broadcast=False)
        if mode == "minutes":
            minutes = int(minutes or self.engine.settings.sleepTimerDefault)
            ends_at = int(time.time()) + max(1, minutes) * 60
            self.sleep = {"mode": "minutes", "endsAt": ends_at}
            delay = max(0.0, ends_at - time.time() - FADE_SECONDS)
            self._sleep_handle = self.engine.loop.call_later(delay, self._sleep_fire)
        elif mode == "episode":
            self.sleep = {"mode": "episode", "endsAt": 0}
        elif mode == "chapter":
            self.sleep = {"mode": "chapter", "endsAt": 0, "chapter": self.mpv.props.get("chapter")}
        else:
            self.sleep = {"mode": "off", "endsAt": 0}
        self.broadcast_player(force=True)
        return self.player_state()

    def _cancel_sleep(self, broadcast=True):
        if self._sleep_handle is not None:
            self._sleep_handle.cancel()
            self._sleep_handle = None
        self.sleep = {"mode": "off", "endsAt": 0}
        if broadcast:
            self.broadcast_player(force=True)

    def _sleep_fire(self):
        self._sleep_handle = None
        self.engine.loop.create_task(self._sleep_fade())

    async def _sleep_fade(self):
        self.sleep = {"mode": "off", "endsAt": 0}
        if not self.loaded or self.mpv.state != "connected":
            self.broadcast_player(force=True)
            return
        volume = int(self.mpv.props.get("volume") or 100)
        self._fading = True
        try:
            for step in range(1, FADE_STEPS + 1):
                await self.mpv.set_property("volume", int(volume * (1 - step / FADE_STEPS)))
                await asyncio.sleep(FADE_SECONDS / FADE_STEPS)
            await self.mpv.set_property("pause", True)
            await self.mpv.set_property("volume", volume)
        except MpvError:
            pass
        finally:
            self._fading = False
        LOG.info("sleep timer paused playback")
        self.engine.notice("info", "Sleep timer: paused")
        self.broadcast_player(force=True)

    # ---- hooks -------------------------------------------------------------

    def _on_new_episodes(self, podcast_id, episode_ids, first_fetch):
        podcast = self.store.one("SELECT auto_queue, title FROM podcasts WHERE id = ?", (podcast_id,))
        if podcast is None or first_fetch:
            return
        if podcast["auto_queue"] in ("next", "last"):
            self.engine.queue.add(episode_ids, podcast["auto_queue"])
        hook = getattr(self.engine, "on_new_episodes_extra", None)
        if hook:
            hook(podcast_id, episode_ids, first_fetch)


def _clamp_speed(value):
    try:
        speed = float(value)
    except (TypeError, ValueError):
        speed = 1.0
    return max(SPEED_MIN, min(SPEED_MAX, round(speed, 3)))


def _media_title(row):
    title = str(row["title"] or "Episode")
    podcast = str(row["podcast_title"] or "")
    return title


# ---------------------------------------------------------------- commands

def _pb(engine):
    return engine.playback


@protocol.command("play", "Play an episode (resumes where it stopped)", episodeId=A(int))
async def cmd_play(engine, client, episodeId):
    return {"player": await _pb(engine).play(episodeId)}


@protocol.command("pause", "Pause")
async def cmd_pause(engine, client):
    await _pb(engine)._set_pause(True)
    return {"player": _pb(engine).player_state()}


@protocol.command("resume", "Resume")
async def cmd_resume(engine, client):
    await _pb(engine)._set_pause(False)
    return {"player": _pb(engine).player_state()}


@protocol.command("toggle", "Play or pause")
async def cmd_toggle(engine, client):
    return {"player": await _pb(engine).toggle()}


@protocol.command("stop", "Stop and unload")
async def cmd_stop(engine, client):
    return {"player": await _pb(engine).stop_playback()}


@protocol.command("seek", "Seek", pos=A(float), mode=A(str, required=False, default="absolute", choices=["absolute", "relative"]))
async def cmd_seek(engine, client, pos, mode):
    return {"player": await _pb(engine).seek(pos, mode)}


@protocol.command("skip", "Skip back or forward by the configured amount", direction=A(str, choices=["back", "forward"]))
async def cmd_skip(engine, client, direction):
    return {"player": await _pb(engine).skip(direction)}


@protocol.command("set-speed", "Playback speed, globally or for one podcast",
                  speed=A(float, minimum=SPEED_MIN, maximum=SPEED_MAX), podcastId=A(int, required=False))
async def cmd_set_speed(engine, client, speed, podcastId):
    return {"player": await _pb(engine).set_speed(speed, podcastId)}


@protocol.command("set-volume", "Volume 0-100", volume=A(int, minimum=0, maximum=100))
async def cmd_set_volume(engine, client, volume):
    return {"player": await _pb(engine).set_volume(volume)}


@protocol.command("set-mute", "Mute", mute=A(bool))
async def cmd_set_mute(engine, client, mute):
    return {"player": await _pb(engine).set_mute(mute)}


@protocol.command("next", "Play the first episode in Up Next")
async def cmd_next(engine, client):
    return {"player": await _pb(engine).next_episode()}


@protocol.command("previous", "Restart the episode")
async def cmd_previous(engine, client):
    return {"player": await _pb(engine).previous()}


@protocol.command("set-chapter", "Jump to a chapter", index=A(int, minimum=0))
async def cmd_set_chapter(engine, client, index):
    return {"player": await _pb(engine).set_chapter(index)}


@protocol.command("sleep-timer", "Sleep timer", mode=A(str, choices=["off", "minutes", "episode", "chapter"]),
                  minutes=A(int, required=False, minimum=1, maximum=720))
def cmd_sleep_timer(engine, client, mode, minutes):
    return {"player": _pb(engine).set_sleep(mode, minutes)}


@protocol.command("set-skip-silence", "Speed through silence", enabled=A(bool))
async def cmd_set_skip_silence(engine, client, enabled):
    return {"player": await _pb(engine).set_skip_silence(enabled)}


@protocol.command("set-voice-boost", "Even out voices", enabled=A(bool))
async def cmd_set_voice_boost(engine, client, enabled):
    return {"player": await _pb(engine).set_voice_boost(enabled)}


@protocol.command("set-position", "Store a position without seeking", episodeId=A(int), pos=A(float, minimum=0))
def cmd_set_position(engine, client, episodeId, pos):
    engine.library.require_episode(episodeId)
    engine.library.set_position(episodeId, pos)
    if _pb(engine).current_id == episodeId and not _pb(engine).loaded:
        _pb(engine).pos = pos
        _pb(engine).broadcast_player(force=True)
    engine.library.emit_episode(episodeId)
    return {"ok": True}
