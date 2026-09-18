import json
import os
import unittest

from engine.transcripts import canonical, parsers, whisper

HERE = os.path.dirname(os.path.abspath(__file__))


def fixture(name):
    with open(os.path.join(HERE, "fixtures", "transcripts", name), "rb") as handle:
        return handle.read()


class ParserTest(unittest.TestCase):
    def test_vtt(self):
        segments, timed = parsers.parse(fixture("sample.vtt"), "text/vtt")
        self.assertTrue(timed)
        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[0]["speaker"], "Alice")
        self.assertEqual(segments[0]["body"], "Hello there, welcome to the show.")
        self.assertEqual(segments[0]["startTime"], 0.5)
        self.assertEqual(segments[1]["endTime"], 8.25)
        self.assertEqual(segments[2]["speaker"], "")

    def test_srt(self):
        segments, timed = parsers.parse(fixture("sample.srt"), "application/x-subrip")
        self.assertTrue(timed)
        self.assertEqual([s["speaker"] for s in segments], ["Alice", "Bob", "Alice"])
        self.assertEqual(segments[2]["body"], "Sure.")

    def test_json(self):
        segments, timed = parsers.parse(fixture("sample.json"), "application/json")
        self.assertTrue(timed)
        self.assertEqual(segments[0]["speaker"], "Alice")
        self.assertEqual(segments[2]["startTime"], 900.0)
        self.assertEqual(segments[2]["body"], "Milliseconds here.")

    def test_html(self):
        segments, timed = parsers.parse(fixture("sample.html"), "text/html")
        self.assertTrue(timed)
        self.assertEqual(segments[0]["startTime"], 0.5)
        self.assertEqual(segments[0]["speaker"], "Alice")
        self.assertEqual(segments[1]["startTime"], 4.0)
        self.assertEqual(segments[1]["speaker"], "Bob")
        self.assertNotIn("bad()", " ".join(s["body"] for s in segments))
        self.assertEqual(segments[2]["body"], "No time on this one.")

    def test_text_and_sniffing(self):
        segments, timed = parsers.parse(fixture("sample.txt"), "text/plain")
        self.assertTrue(timed)
        self.assertEqual(segments[1]["startTime"], 4.0)
        self.assertEqual(segments[1]["speaker"], "Bob")
        # A VTT served as text/plain is still recognised.
        segments, _ = parsers.parse(fixture("sample.vtt"), "text/plain")
        self.assertEqual(segments[0]["speaker"], "Alice")
        untimed, timed = parsers.parse(b"Just some words.\n\nAnd more.", "text/plain")
        self.assertFalse(timed)
        self.assertEqual(len(untimed), 2)

    def test_choose_source(self):
        sources = [
            {"url": "https://x/t.srt", "type": "application/x-subrip"},
            {"url": "https://x/t.vtt", "type": "text/vtt", "language": "de"},
            {"url": "https://x/t.json", "type": "application/json", "language": "en"},
            {"url": "ftp://x/t.vtt", "type": "text/vtt"},
        ]
        self.assertEqual(parsers.choose_source(sources, "en")["url"], "https://x/t.json")
        self.assertEqual(parsers.choose_source(sources, "de")["url"], "https://x/t.vtt")
        self.assertIsNone(parsers.choose_source([], "en"))


class CanonicalTest(unittest.TestCase):
    def test_paragraphs_merge_by_speaker(self):
        segments = [
            canonical.segment(0, 2, "Hello there", "A"),
            canonical.segment(2, 4, "how are you?", "A"),
            canonical.segment(4, 6, "Fine.", "B"),
            canonical.segment(6, 8, "Great", "B"),
        ]
        merged = canonical.paragraphs(segments)
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[0]["body"], "Hello there how are you?")
        self.assertEqual(merged[0]["endTime"], 4)
        self.assertEqual(merged[1]["body"], "Fine.")

    def test_key_and_roundtrip(self):
        key = canonical.episode_key("https://feed", "guid-1")
        self.assertEqual(len(key), 24)
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.json")
            doc = canonical.new_document("feed", "text/vtt", "en", True, "complete", "", [canonical.segment(0, 1, "hi")])
            canonical.save(path, doc)
            self.assertEqual(canonical.load(path)["segments"][0]["body"], "hi")
            self.assertIsNone(canonical.load(os.path.join(tmp, "missing.json")))


class WhisperTest(unittest.TestCase):
    def test_chunk_plan_and_merge(self):
        plan = whisper.plan_chunks(1250)
        self.assertEqual([p[0] for p in plan], [0, 600, 1200])
        self.assertEqual(plan[0][1], 604)
        self.assertEqual(plan[-1][1], 50)
        first = [canonical.segment(0, 5, "a"), canonical.segment(596, 601, "b")]
        second = [canonical.segment(598, 602, "b again"), canonical.segment(603, 610, "c")]
        merged = whisper.merge_chunk(first, second, 600)
        self.assertEqual([s["body"] for s in merged], ["a", "b", "c"])

    def test_result_parsing_filters_hallucinations(self):
        payload = json.loads(fixture("whisper-chunk.json"))
        kept = [item["text"].strip() for item in payload["transcription"] if not whisper._looks_like_hallucination(item["text"])]
        self.assertEqual(kept, ["Hello there.", "Sure."])

    def test_detect_shape(self):
        info = whisper.detect("/nonexistent")
        for key in ("available", "gpu", "model", "modelFile", "modelPresent", "ffmpeg"):
            self.assertIn(key, info)
        self.assertGreater(whisper.estimate_seconds(3600, {"gpu": True, "model": "large-v3-turbo"}), 0)


if __name__ == "__main__":
    unittest.main()


class ModelDownloadTest(unittest.TestCase):
    """download_model against a fake Hugging Face: pinned size + SHA-256."""

    BODY = bytes(range(256)) * 800     # 204800 bytes

    def setUp(self):
        import hashlib
        import tempfile
        from unittest import mock
        from tests.fakes import FakeHttpServer
        self.http = FakeHttpServer()
        self.tmp = tempfile.TemporaryDirectory()
        self.models_dir = os.path.join(self.tmp.name, "models")
        self.entry = whisper.Model("ggml-tiny-q5_1.bin", len(self.BODY), hashlib.sha256(self.BODY).hexdigest(), False)
        patches = [
            mock.patch.dict(whisper.MODELS, {"tiny": self.entry}),
            mock.patch.object(whisper, "MODEL_BASE_URL", self.http.base + "/"),
            mock.patch.object(whisper, "MODEL_REVISION", "rev"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.http.close)
        self.phases = []

    def progress(self, done, total, phase="downloading"):
        self.phases.append(phase)

    def target(self):
        return os.path.join(self.models_dir, self.entry.file)

    def leftovers(self):
        try:
            return sorted(name for name in os.listdir(self.models_dir) if name != self.entry.file)
        except FileNotFoundError:
            return []

    def gets(self):
        return [req for req in self.http.requests if req[0] == "GET"]

    def test_good_download_installs_and_verifies(self):
        self.http.add("/rev/ggml-tiny-q5_1.bin", self.BODY)
        path = whisper.download_model(self.models_dir, "tiny", self.progress)
        self.assertEqual(path, self.target())
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), self.BODY)
        self.assertEqual(self.leftovers(), [])
        self.assertIn("verifying", self.phases)
        self.assertEqual(self.phases[-1], "done")
        self.assertTrue(whisper.verify_model(path, self.entry))
        # A second call is satisfied by the verified file: no new request.
        before = len(self.http.requests)
        whisper.download_model(self.models_dir, "tiny", self.progress)
        self.assertEqual(len(self.http.requests), before)

    def test_bad_digest_is_rejected(self):
        self.http.add("/rev/ggml-tiny-q5_1.bin", self.BODY[:-1] + b"\x00")
        with self.assertRaises(whisper.TranscribeError) as caught:
            whisper.download_model(self.models_dir, "tiny", self.progress)
        self.assertIn("integrity", str(caught.exception))
        self.assertFalse(os.path.exists(self.target()))
        self.assertEqual(self.leftovers(), [])

    def test_resume_after_drop(self):
        from engine import http
        self.http.add("/rev/ggml-tiny-q5_1.bin", self.BODY)
        self.http.fail_after_bytes = 60000
        with self.assertRaises(http.FetchError):
            whisper.download_model(self.models_dir, "tiny", self.progress)
        left = self.leftovers()
        self.assertEqual(len(left), 2, left)             # the .part and its sidecar
        self.assertTrue(any(name.endswith(".part") for name in left))
        self.assertTrue(any(name.endswith(".download.json") for name in left))
        self.http.fail_after_bytes = None
        path = whisper.download_model(self.models_dir, "tiny", self.progress)
        self.assertTrue(whisper.verify_model(path, self.entry))
        self.assertEqual(self.leftovers(), [])
        second = self.gets()[-1]
        self.assertEqual(second[2].get("Range"), "bytes=60000-")

    def test_declared_size_mismatch_is_refused(self):
        from engine import http
        self.http.add("/rev/ggml-tiny-q5_1.bin", self.BODY + b"extra")
        with self.assertRaises(http.FetchError) as caught:
            whisper.download_model(self.models_dir, "tiny", self.progress)
        self.assertEqual(caught.exception.kind, "too-large")
        self.assertEqual(self.leftovers(), [])

    def test_oversized_body_without_length_is_capped(self):
        from engine import http
        self.http.add("/rev/ggml-tiny-q5_1.bin", self.BODY + b"extra", content_length=False)
        with self.assertRaises(http.FetchError) as caught:
            whisper.download_model(self.models_dir, "tiny", self.progress)
        self.assertEqual(caught.exception.kind, "too-large")
        self.assertEqual(self.leftovers(), [])

    def test_stale_sidecar_is_ignored(self):
        os.makedirs(self.models_dir)
        stale = os.path.join(self.models_dir, "ggml-tiny-q5_1.bin.0123456789abcdef.part")
        with open(stale, "wb") as handle:
            handle.write(b"x" * 10)
        with open(os.path.join(self.models_dir, "ggml-tiny-q5_1.bin.download.json"), "w") as handle:
            json.dump({"temp": os.path.basename(stale), "size": self.entry.size, "sha256": "0" * 64, "revision": "rev"}, handle)
        self.http.add("/rev/ggml-tiny-q5_1.bin", self.BODY)
        path = whisper.download_model(self.models_dir, "tiny", self.progress)
        self.assertTrue(whisper.verify_model(path, self.entry))
        self.assertEqual(self.leftovers(), [])
        self.assertNotIn("Range", self.gets()[0][2])

    def _plant_partial(self):
        os.makedirs(self.models_dir)
        temp = os.path.join(self.models_dir, "ggml-tiny-q5_1.bin.0123456789abcdef.part")
        with open(temp, "wb") as handle:
            handle.write(self.BODY[:10])
        with open(os.path.join(self.models_dir, "ggml-tiny-q5_1.bin.download.json"), "w") as handle:
            json.dump({"temp": os.path.basename(temp), "size": self.entry.size, "sha256": self.entry.sha256, "revision": "rev"}, handle)

    def test_ignored_range_restarts_from_zero(self):
        self._plant_partial()
        self.http.add("/rev/ggml-tiny-q5_1.bin", self.BODY, ranges=False)
        path = whisper.download_model(self.models_dir, "tiny", self.progress)
        self.assertTrue(whisper.verify_model(path, self.entry))
        self.assertEqual(self.gets()[0][2].get("Range"), "bytes=10-")

    def test_wrong_content_range_restarts(self):
        self._plant_partial()
        self.http.add("/rev/ggml-tiny-q5_1.bin", self.BODY, bad_range=True)
        path = whisper.download_model(self.models_dir, "tiny", self.progress)
        self.assertTrue(whisper.verify_model(path, self.entry))
        self.assertEqual(self.leftovers(), [])
        # first attempt asked for a resume, the retry started fresh
        ranges = [req[2].get("Range") for req in self.gets()]
        self.assertEqual(ranges, ["bytes=10-", None])

    def test_probe_mismatch_fails_before_transfer(self):
        self.http.add("/rev/ggml-tiny-q5_1.bin", self.BODY, extra_headers={"x-linked-etag": '"' + "0" * 64 + '"'})
        with self.assertRaises(whisper.TranscribeError) as caught:
            whisper.download_model(self.models_dir, "tiny", self.progress)
        self.assertIn("pinned digest", str(caught.exception))
        self.assertEqual([req[0] for req in self.http.requests], ["HEAD"])
        self.assertEqual(self.leftovers(), [])

    def test_corrupt_existing_model_is_replaced(self):
        os.makedirs(self.models_dir)
        with open(self.target(), "wb") as handle:
            handle.write(b"junk" * 100)
        with open(self.target() + ".part", "wb") as handle:
            handle.write(b"old predictable temp")
        self.http.add("/rev/ggml-tiny-q5_1.bin", self.BODY)
        path = whisper.download_model(self.models_dir, "tiny", self.progress)
        self.assertTrue(whisper.verify_model(path, self.entry))
        self.assertEqual(self.leftovers(), [])

    def test_verify_model_and_detect(self):
        os.makedirs(self.models_dir)
        with open(self.target(), "wb") as handle:
            handle.write(self.BODY)
        self.assertTrue(whisper.verify_model(self.target(), self.entry))
        with open(self.target(), "r+b") as handle:
            handle.seek(5)
            handle.write(b"\xff")
        self.assertFalse(whisper.verify_model(self.target(), self.entry))      # right size, wrong bytes
        link = os.path.join(self.models_dir, "link.bin")
        os.symlink(self.target(), link)
        self.assertFalse(whisper.verify_model(link, self.entry))              # never through a symlink
        with open(self.target(), "ab") as handle:
            handle.write(b"x")
        self.assertFalse(whisper.verify_model(self.target(), self.entry))      # wrong size
        info = whisper.detect(self.models_dir)
        self.assertEqual(sorted(info.keys()), ["available", "binary", "cpus", "ffmpeg", "gpu", "model", "modelFile",
                                               "modelPresent", "vram", "vulkan"])

    def test_cancel_and_deadline_kill_the_process_group(self):
        import subprocess
        import tempfile
        import threading
        import time
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        with tempfile.TemporaryFile() as errlog:
            with self.assertRaises(whisper.Cancelled):
                whisper._run(["sh", "-c", "sleep 60 & sleep 60"], errlog, cancel, 30)
        with tempfile.TemporaryFile() as errlog:
            started = time.monotonic()
            with self.assertRaises(whisper.TranscribeError) as caught:
                whisper._run(["sh", "-c", "sleep 60"], errlog, None, 0.5)
            self.assertLess(time.monotonic() - started, 10)
            self.assertIn("time limit", str(caught.exception))
        self.assertEqual(whisper._CHILDREN, set())
