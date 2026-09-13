import json
import unittest

from engine import chapters


class ChaptersTest(unittest.TestCase):
    def test_parse_json_chapters(self):
        doc = json.dumps({"version": "1.2.0", "chapters": [
            {"startTime": 0, "title": "Intro", "img": "https://x/a.jpg"},
            {"startTime": 120.5, "title": "Hidden", "toc": False},
            {"startTime": 60, "title": "Topic", "url": "javascript:x", "endTime": 90},
            {"startTime": "bad"},
        ]}).encode()
        parsed = chapters.parse_json_chapters(doc)
        self.assertEqual([c["title"] for c in parsed], ["Intro", "Topic"])
        self.assertEqual(parsed[1]["endTime"], 90)
        self.assertEqual(parsed[1]["url"], "")
        self.assertEqual(parsed[0]["img"], "https://x/a.jpg")
        self.assertEqual(chapters.parse_json_chapters(b"nope"), [])

    def test_merge_prefers_feed_titles_and_fills_ends(self):
        embedded = [{"startTime": 0, "title": "ID3 intro"}, {"startTime": 61, "title": "ID3 two"}, {"startTime": 300, "title": "ID3 three"}]
        external = [{"startTime": 0, "title": "Intro"}, {"startTime": 60, "title": ""}]
        merged = chapters.merge(embedded, external, duration=400)
        self.assertEqual([c["title"] for c in merged], ["Intro", "ID3 two", "ID3 three"])
        self.assertEqual([c["endTime"] for c in merged], [60, 300, 400])
        self.assertEqual([c["index"] for c in merged], [0, 1, 2])
        alone = chapters.merge([], [{"startTime": 5}], None)
        self.assertEqual(alone[0]["title"], "Chapter 1")
        self.assertIsNone(alone[0]["endTime"])


if __name__ == "__main__":
    unittest.main()
