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
