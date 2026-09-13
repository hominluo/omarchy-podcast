"""The transcripts subsystem: answers `transcript-get`, runs local
transcriptions one at a time, and applies the transcription policy.

Feed transcripts are fetched and parsed on first request and cached as the
canonical document. When there is none, `transcribe` queues the episode:
the audio is fetched into the cache if needed, converted, and run through
whisper.cpp chunk by chunk, with every chunk's segments broadcast as they
land so the view fills in progressively.
"""

import asyncio
import json
import os
import threading

from .. import http, log, protocol
from ..store import now
from . import canonical, parsers, whisper

LOG = log.get("transcripts")
A = protocol.Arg


class Transcripts:
    def __init__(self, engine):
        self.engine = engine
        self.store = None
        self.info = {}
        self.pending = []           # episode ids waiting
        self.current = None         # episode id being transcribed
        self._cancel = None
        self._task = None
        self._model_task = None

    # ---- lifecycle ---------------------------------------------------------

    async def start(self):
        self.store = self.engine.store
        self._detect()
        self.engine.on_settings_changed(self._detect)
        self.engine.on_download_done = self._on_download_done
        self.engine.on_queued = self._on_queued
        for row in self.store.all("SELECT episode_id FROM transcripts WHERE status = 'queued' ORDER BY created_at"):
            self.pending.append(row["episode_id"])
        self.store.execute("UPDATE transcripts SET status = 'queued' WHERE status = 'partial' AND source = 'whisper'")
        self._broadcast_jobs()
        self._kick()

    async def stop(self, restart=False, quit_mpv=True):
        if self._cancel is not None:
            self._cancel.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _detect(self):
        self.info = whisper.detect(self.engine.paths.models_dir, self.engine.settings)
        self.engine.update_state("engine", whisper={
            "available": self.info["available"] and self.info["ffmpeg"],
            "gpu": self.info["gpu"],
            "model": self.info["model"],
            "modelPresent": self.info["modelPresent"],
            "policy": self.engine.settings.transcriptionPolicy,
        })

    def _broadcast_jobs(self):
        ids = ([self.current] if self.current is not None else []) + list(self.pending)
        self.engine.update_state("jobs", transcribing=ids)

    # ---- helpers -----------------------------------------------------------

    def _key(self, row):
        return canonical.episode_key(row["podcast_feed_url"], row["guid"])

    def _doc_path(self, row):
        return canonical.path_for(self.engine.paths, self._key(row))

    def _row(self, episode_id):
        return self.store.one("SELECT * FROM transcripts WHERE episode_id = ?", (int(episode_id),))

    def _upsert(self, episode_id, **fields):
        stamp = now()
        existing = self._row(episode_id)
        if existing is None:
            columns = {"source": "feed", "source_url": "", "source_type": "", "language": "", "path": "",
                       "segment_count": 0, "status": "queued", "progress_sec": 0, "model": "", "error": "",
                       "created_at": stamp, "updated_at": stamp}
            columns.update(fields)
            keys = ", ".join(["episode_id"] + list(columns.keys()))
            marks = ", ".join(["?"] * (len(columns) + 1))
            self.store.execute("INSERT INTO transcripts (%s) VALUES (%s)" % (keys, marks), [int(episode_id)] + list(columns.values()))
        else:
            fields["updated_at"] = stamp
            sets = ", ".join("%s = ?" % key for key in fields)
            self.store.execute("UPDATE transcripts SET %s WHERE episode_id = ?" % sets, list(fields.values()) + [int(episode_id)])

    def _response(self, doc, status, granularity, extra=None):
        segments = doc["segments"] if doc else []
        if granularity == "paragraphs":
            segments = canonical.paragraphs(segments)
        payload = {
            "status": status,
            "source": doc.get("source", "") if doc else "",
            "sourceType": doc.get("sourceType", "") if doc else "",
            "language": doc.get("language", "") if doc else "",
            "timed": doc.get("timed", True) if doc else False,
            "model": doc.get("model", "") if doc else "",
            "segments": segments,
        }
        if extra:
            payload.update(extra)
        return payload

    # ---- transcript-get ----------------------------------------------------

    async def get(self, episode_id, granularity="paragraphs"):
        row = self.engine.library.require_episode(episode_id)
        trow = self._row(episode_id)
        if trow is not None and trow["status"] in ("complete", "partial") and trow["path"]:
            doc = canonical.load(trow["path"])
            if doc is not None:
                extra = {}
                if trow["status"] == "partial":
                    duration = float(row["duration"] or 0)
                    extra["percent"] = min(99, int(100 * float(trow["progress_sec"]) / duration)) if duration > 0 else 0
                    extra["queued"] = episode_id in self.pending
                    extra["running"] = self.current == episode_id
                return self._response(doc, trow["status"], granularity, extra)
        if trow is not None and trow["status"] == "queued":
            return self._response(None, "queued", granularity, {"percent": 0, "queued": True, "running": self.current == episode_id})

        sources = []
        try:
            sources = json.loads(row["transcripts_json"] or "[]")
        except ValueError:
            sources = []
        chosen = parsers.choose_source(sources, row["podcast_language"])
        if chosen is not None:
            doc = await self._fetch_feed_transcript(row, chosen)
            if doc is not None:
                return self._response(doc, "complete", granularity)

        policy = self.engine.settings.transcriptionPolicy
        can = bool(self.info.get("available")) and bool(self.info.get("ffmpeg")) and policy != "off"
        return {
            "status": "none", "segments": [], "canTranscribe": can,
            "estimateSec": whisper.estimate_seconds(row["duration"], self.info) if can else 0,
            "model": self.info.get("model", ""), "gpu": bool(self.info.get("gpu")),
            "reason": "" if can else ("Transcription is off in settings" if policy == "off" else "whisper-cli is not installed (omarchy pkg add whisper-cpp ggml-vulkan)"),
            "error": trow["error"] if trow is not None and trow["status"] == "error" else "",
        }

    async def _fetch_feed_transcript(self, row, source):
        url = source["url"]
        try:
            response = await self.engine.run_in_thread(http.fetch, url, parsers.MAX_BYTES, 30)
        except http.FetchError as error:
            LOG.info("transcript fetch for episode %d failed: %s", row["id"], error.message)
            return None
        segments, timed = await self.engine.run_in_thread(parsers.parse, response.body, source.get("type") or response.content_type)
        if not segments:
            return None
        doc = canonical.new_document("feed", source.get("type") or response.content_type, source.get("language") or row["podcast_language"],
                                     timed, "complete", "", segments)
        path = self._doc_path(row)
        await self.engine.run_in_thread(canonical.save, path, doc)
        self._upsert(row["id"], source="feed", source_url=url, source_type=doc["sourceType"], language=doc["language"],
                     path=path, segment_count=len(segments), status="complete", progress_sec=float(row["duration"] or 0), error="")
        self.engine.library.emit_episode(row["id"])
        return doc

    # ---- transcribe --------------------------------------------------------

    def transcribe(self, episode_id):
        row = self.engine.library.require_episode(episode_id)
        if self.engine.settings.transcriptionPolicy == "off":
            raise protocol.ProtocolError(protocol.UNAVAILABLE, "Transcription is turned off in settings")
        if not self.info.get("available") or not self.info.get("ffmpeg"):
            raise protocol.ProtocolError(protocol.UNAVAILABLE, "whisper-cli is not installed; run `omarchy pkg add whisper-cpp ggml-vulkan`")
        trow = self._row(episode_id)
        if trow is not None and trow["status"] == "complete":
            return {"status": "complete"}
        if episode_id == self.current or episode_id in self.pending:
            return {"status": "queued"}
        self._upsert(row["id"], source="whisper", status="queued", model=self.info["model"], error="")
        self.pending.append(row["id"])
        self._broadcast_jobs()
        self.engine.library.emit_episode(row["id"])
        self._kick()
        return {"status": "queued", "estimateSec": whisper.estimate_seconds(row["duration"], self.info)}

    def cancel(self, episode_id):
        episode_id = int(episode_id)
        if episode_id in self.pending:
            self.pending.remove(episode_id)
            self.store.execute("DELETE FROM transcripts WHERE episode_id = ? AND status = 'queued'", (episode_id,))
        if self.current == episode_id and self._cancel is not None:
            self._cancel.set()
        self._broadcast_jobs()
        self.engine.library.emit_episode(episode_id)
        return {"cancelled": episode_id}

    def delete(self, episode_id):
        trow = self._row(episode_id)
        if trow is not None and trow["path"]:
            try:
                os.unlink(trow["path"])
            except OSError:
                pass
        self.store.execute("DELETE FROM transcripts WHERE episode_id = ?", (int(episode_id),))
        self.engine.library.emit_episode(episode_id)
        return {"deleted": int(episode_id)}

    def _kick(self):
        if self.current is None and self.pending and (self._task is None or self._task.done()):
            self._task = asyncio.ensure_future(self._run_next())

    async def _run_next(self):
        while self.pending:
            episode_id = self.pending.pop(0)
            self.current = episode_id
            self._cancel = threading.Event()
            self._broadcast_jobs()
            try:
                await self._transcribe(episode_id)
            except whisper.Cancelled:
                LOG.info("transcription of episode %d cancelled", episode_id)
                self._upsert(episode_id, status="error", error="cancelled")
                self._progress(episode_id, "error", 0, [], error="cancelled")
            except (whisper.TranscribeError, protocol.ProtocolError) as error:
                message = getattr(error, "message", None) or str(error)
                LOG.warning("transcription of episode %d failed: %s", episode_id, message)
                self._upsert(episode_id, status="error", error=message[:200])
                self._progress(episode_id, "error", 0, [], error=message)
                self.engine.notice("error", "Transcription failed: %s" % message, episode_id=episode_id)
            except Exception as error:  # noqa: BLE001
                LOG.exception("transcription of episode %d crashed", episode_id)
                self._upsert(episode_id, status="error", error=str(error)[:200])
                self._progress(episode_id, "error", 0, [], error=str(error))
            finally:
                self.current = None
                self._cancel = None
                self.engine.library.emit_episode(episode_id)
                self._broadcast_jobs()

    def _progress(self, episode_id, stage, percent, segments, **extra):
        payload = {"episodeId": episode_id, "stage": stage, "percent": int(percent), "segments": segments}
        payload.update(extra)
        self.engine.emit("transcript-progress", payload)

    async def _ensure_model(self):
        model = self.info["model"]
        path = whisper.model_path(self.engine.paths.models_dir, model)
        if whisper.model_ok(path, model):
            return path

        def progress(done, total):
            self.engine.call_soon(self.engine.update_state, "jobs", modelDownload={"model": model, "percent": int(100 * done / max(1, total)), "done": done, "total": total})

        self.engine.notice("info", "Downloading the %s speech model (%d MB) — first time only" % (model, whisper.MODELS[model][1] // (1024 * 1024)))
        try:
            path = await self.engine.run_in_thread(whisper.download_model, self.engine.paths.models_dir, model, progress, self._cancel)
        except http.FetchError as error:
            raise whisper.TranscribeError("could not download the model: %s" % error.message)
        finally:
            self.engine.update_state("jobs", modelDownload=None)
        self._detect()
        return path

    async def _ensure_audio(self, row):
        downloads = self.engine.downloads
        state = self.store.one("SELECT status, path FROM downloads WHERE episode_id = ?", (row["id"],))
        if state is not None and state["status"] == "done" and state["path"] and os.path.exists(state["path"]):
            return state["path"]
        self._progress(row["id"], "fetching", 0, [])
        downloads.request([row["id"]], keep=0, priority=2)
        path = await downloads.wait_for(row["id"])
        if not path:
            raise whisper.TranscribeError("could not fetch the audio")
        return path

    async def _transcribe(self, episode_id):
        row = self.engine.library.require_episode(episode_id)
        model_path = await self._ensure_model()
        audio = await self._ensure_audio(row)
        if self._cancel.is_set():
            raise whisper.Cancelled()

        wav = os.path.join(self.engine.paths.audio_dir, "%d.wav" % episode_id)
        self._progress(episode_id, "converting", 0, [])
        duration = await self.engine.run_in_thread(whisper.convert_to_wav, audio, wav, self._cancel)
        if duration <= 0:
            raise whisper.TranscribeError("the audio is empty")

        doc_path = self._doc_path(row)
        # The feed's language when it states one; otherwise whisper detects it
        # on the first chunk and the rest of the run sticks with that.
        feed_language = str(row["podcast_language"] or "").split("-")[0].lower()
        language = feed_language if feed_language and feed_language != "und" else "auto"
        doc = canonical.new_document("whisper", "", "" if language == "auto" else language, True, "partial", self.info["model"], [])
        self._upsert(episode_id, source="whisper", path=doc_path, status="partial", progress_sec=0, model=self.info["model"], language=doc["language"])
        segments = []
        gpu = bool(self.info["gpu"])
        threads = max(1, (self.info.get("cpus") or 2) // 2)
        try:
            for offset, length in whisper.plan_chunks(duration):
                if self._cancel.is_set():
                    raise whisper.Cancelled()
                try:
                    chunk, detected = await self.engine.run_in_thread(
                        whisper.transcribe_chunk, self.info["binary"], model_path, wav, offset, length, language, gpu, threads, self._cancel)
                except whisper.TranscribeError as error:
                    if gpu:
                        LOG.warning("GPU run failed (%s); retrying this chunk on the CPU", error)
                        gpu = False
                        chunk, detected = await self.engine.run_in_thread(
                            whisper.transcribe_chunk, self.info["binary"], model_path, wav, offset, length, language, gpu, threads, self._cancel)
                    else:
                        raise
                if language == "auto" and detected:
                    language = detected
                    doc["language"] = detected
                before = len(segments)
                segments = whisper.merge_chunk(segments, chunk, offset)
                fresh = segments[before:]
                doc["segments"] = segments
                processed = min(duration, offset + length)
                await self.engine.run_in_thread(canonical.save, doc_path, doc)
                self._upsert(episode_id, progress_sec=processed, segment_count=len(segments), language=doc["language"])
                self._progress(episode_id, "transcribing", 100 * processed / duration, fresh, language=doc["language"])
            doc["status"] = "complete"
            await self.engine.run_in_thread(canonical.save, doc_path, doc)
            self._upsert(episode_id, status="complete", progress_sec=duration, segment_count=len(segments), error="")
            self._progress(episode_id, "done", 100, [], language=doc["language"])
            LOG.info("transcribed episode %d: %d segments (%s, %s)", episode_id, len(segments), self.info["model"], "gpu" if gpu else "cpu")
            self.engine.notice("info", "Transcript ready: %s" % str(row["title"])[:60], episode_id=episode_id)
        finally:
            try:
                os.unlink(wav)
            except OSError:
                pass
            cached = self.store.one("SELECT keep FROM downloads WHERE episode_id = ?", (episode_id,))
            if cached is not None and not cached["keep"]:
                self.engine.downloads.delete([episode_id])

    # ---- policy hooks ------------------------------------------------------

    def _wants_auto(self, episode_id):
        trow = self._row(episode_id)
        if trow is not None and trow["status"] in ("complete", "partial", "queued"):
            return False
        row = self.engine.library.episode_row(episode_id)
        if row is None or row["transcripts_json"] not in ("", "[]"):
            return False  # the feed has one; it is fetched on demand
        return bool(self.info.get("available"))

    def _on_download_done(self, episode_id, keep):
        if keep and self.engine.settings.transcriptionPolicy in ("downloaded", "queued") and self._wants_auto(episode_id):
            try:
                self.transcribe(episode_id)
            except protocol.ProtocolError:
                pass

    def _on_queued(self, episode_ids):
        if self.engine.settings.transcriptionPolicy != "queued":
            return
        for episode_id in episode_ids:
            if self._wants_auto(episode_id):
                try:
                    self.transcribe(episode_id)
                except protocol.ProtocolError:
                    pass


# ---------------------------------------------------------------- commands

@protocol.command("transcript-get", "The transcript for an episode, fetching the feed's if needed",
                  episodeId=A(int), granularity=A(str, required=False, default="paragraphs", choices=["cues", "paragraphs"]))
async def cmd_transcript_get(engine, client, episodeId, granularity):
    return await engine.transcripts.get(episodeId, granularity)


@protocol.command("transcribe", "Transcribe locally with whisper.cpp", episodeId=A(int))
def cmd_transcribe(engine, client, episodeId):
    return engine.transcripts.transcribe(episodeId)


@protocol.command("transcribe-cancel", "Stop a transcription", episodeId=A(int))
def cmd_transcribe_cancel(engine, client, episodeId):
    return engine.transcripts.cancel(episodeId)


@protocol.command("transcript-delete", "Forget a transcript", episodeId=A(int))
def cmd_transcript_delete(engine, client, episodeId):
    return engine.transcripts.delete(episodeId)


@protocol.command("whisper-download-model", "Fetch a speech model ahead of time", model=A(str, required=False))
async def cmd_whisper_download_model(engine, client, model):
    transcripts = engine.transcripts
    if model and model in whisper.MODELS:
        transcripts.info["model"] = model
    transcripts._cancel = threading.Event()
    path = await transcripts._ensure_model()
    return {"model": transcripts.info["model"], "path": path}
