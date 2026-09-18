import os
import unittest

from engine import feeds, htmlclean

HERE = os.path.dirname(os.path.abspath(__file__))


def fixture(name):
    with open(os.path.join(HERE, "fixtures", "feeds", name), "rb") as handle:
        return handle.read()


class FeedParseTest(unittest.TestCase):
    def test_podcasting20_feed(self):
        parsed = feeds.parse(fixture("podcasting20.xml"), "https://example.com/feed")
        podcast = parsed["podcast"]
        self.assertEqual(podcast["title"], "Example Show")
        self.assertEqual(podcast["author"], "Jane Host")
        self.assertEqual(podcast["language"], "en-us")
        self.assertEqual(podcast["image_url"], "https://example.com/art.jpg")
        self.assertEqual(podcast["podcast_guid"], "917393e3-1b1e-5cef-ace4-edaa54e1f810")
        self.assertEqual(podcast["funding"][0]["url"], "https://example.com/support")
        self.assertIn("<b>things</b>", podcast["description_html"])
        self.assertNotIn("script", podcast["description_html"])

        episodes = parsed["episodes"]
        self.assertEqual([e["title"] for e in episodes], ["Episode 2: Tags", "Episode 1: No guid", "Video only"])

        ep2 = episodes[0]
        self.assertEqual(ep2["guid"], "ep-2")
        self.assertEqual(ep2["duration"], 3723)
        self.assertEqual(ep2["pub_date"], 1788343200)
        self.assertEqual(ep2["enclosure_length"], 12345)
        self.assertEqual(ep2["episode_number"], 2)
        self.assertEqual(ep2["season"], 1)
        self.assertEqual(ep2["image_url"], "https://example.com/ep2.jpg")
        self.assertEqual([t["type"] for t in ep2["transcripts"]], ["text/vtt", "application/json"])
        self.assertEqual(ep2["chapters_url"], "https://example.com/ep2.chapters.json")
        self.assertEqual(ep2["persons"][0]["name"], "Jane Host")
        self.assertIn('<a href="https://example.com/x">a link</a>', ep2["notes_html"])
        self.assertNotIn("img", ep2["notes_html"])
        self.assertNotIn("iframe", ep2["notes_html"])
        self.assertNotIn("onclick", ep2["notes_html"])
        self.assertTrue(ep2["content_hash"])

        ep1 = episodes[1]
        self.assertEqual(ep1["guid"], "https://cdn.example.com/ep1.mp3")
        self.assertEqual(ep1["duration"], 1830)
        self.assertEqual(ep1["notes_html"], "<p>Plain text notes.</p><p>Second paragraph.</p>")

        video = episodes[2]
        self.assertEqual(video["enclosure_type"], "video/mp4")

    def test_bad_encoding_and_dates_recover(self):
        parsed = feeds.parse(fixture("bad-encoding.xml"), "https://example.com/bad")
        self.assertEqual(parsed["podcast"]["title"], "Café & Co")
        ep = parsed["episodes"][0]
        self.assertEqual(ep["pub_date"], 1672740000)
        self.assertEqual(ep["duration"], 2710)
        self.assertEqual(ep["enclosure_type"], "audio/x-m4a")

    def test_atom(self):
        parsed = feeds.parse(fixture("atom.xml"), "https://atom.example/feed")
        self.assertEqual(parsed["podcast"]["title"], "Atom Cast")
        self.assertEqual(parsed["podcast"]["author"], "Atom Author")
        self.assertEqual(parsed["podcast"]["link"], "https://atom.example/")
        ep = parsed["episodes"][0]
        self.assertEqual(ep["guid"], "urn:uuid:1")
        self.assertEqual(ep["enclosure_url"], "https://atom.example/1.mp3")
        self.assertEqual(ep["pub_date"], 1785585600)

    def test_item_cap(self):
        items = "".join('<item><title>%d</title><guid>g%d</guid><enclosure url="https://x/%d.mp3" type="audio/mpeg"/></item>' % (i, i, i) for i in range(50))
        data = ('<rss version="2.0"><channel><title>Big</title>%s</channel></rss>' % items).encode()
        parsed = feeds.parse(data, "", max_items=10)
        self.assertEqual(len(parsed["episodes"]), 10)
        self.assertEqual(parsed["podcast"]["title"], "Big")

    def test_not_a_feed(self):
        with self.assertRaises(feeds.FeedParseError):
            feeds.parse(b"<html><body>nope</body></html>", "")
        with self.assertRaises(feeds.FeedParseError):
            feeds.parse(b"garbage", "")


class HelpersTest(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(feeds.parse_duration("1:02:03"), 3723)
        self.assertEqual(feeds.parse_duration("45:10"), 2710)
        self.assertEqual(feeds.parse_duration("3600"), 3600)
        self.assertEqual(feeds.parse_duration("12.5"), 12)
        self.assertEqual(feeds.parse_duration("00:59"), 59)
        self.assertEqual(feeds.parse_duration("1h 5min"), 3900)
        self.assertIsNone(feeds.parse_duration("soon"))
        self.assertIsNone(feeds.parse_duration(""))

    def test_dates(self):
        self.assertEqual(feeds.parse_date("Wed, 02 Sep 2026 10:00:00 +0000"), 1788343200)
        self.assertEqual(feeds.parse_date("2026-09-02T10:00:00Z"), 1788343200)
        self.assertEqual(feeds.parse_date("2026-09-02T12:00:00+02:00"), 1788343200)
        self.assertEqual(feeds.parse_date("Mon, 02 Sep 2026 10:00 GMT"), 1788343200)
        self.assertIsNone(feeds.parse_date("yesterday"))


class HtmlCleanTest(unittest.TestCase):
    def test_whitelist(self):
        rich, text = htmlclean.clean('<p style="x">Hi <em>there</em> <span>friend</span></p><h1>Big</h1><table><tr><td>cell</td></tr></table>')
        self.assertEqual(rich, "<p>Hi <em>there</em> friend</p><h3>Big</h3><p>cell</p>")
        self.assertIn("Hi there friend", text)
        self.assertIn("cell", text)

    def test_dangerous_bits_go_away(self):
        rich, _ = htmlclean.clean('<p>a<script>x()</script><style>p{}</style><img src="x"><a href="javascript:alert(1)">j</a><a href="mailto:a@b">m</a></p>')
        self.assertEqual(rich, '<p>aj<a href="mailto:a@b">m</a></p>')

    def test_unclosed_and_broken(self):
        rich, _ = htmlclean.clean("<p><b>bold<i>both")
        self.assertEqual(rich, "<p><b>bold<i>both</i></b></p>")

    def test_summary(self):
        self.assertEqual(htmlclean.summary("a  b\n c", 100), "a b c")
        self.assertEqual(len(htmlclean.summary("x" * 300, 50)), 50)


if __name__ == "__main__":
    unittest.main()


class BoundsTest(unittest.TestCase):
    def test_doctype_is_rejected(self):
        with self.assertRaises(feeds.FeedParseError):
            feeds.parse(b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaa">]><rss version="2.0"><channel><title>&a;</title></channel></rss>', "")
        with self.assertRaises(feeds.FeedParseError):
            feeds.parse(b'<!-- c --> <!doctype rss><rss version="2.0"><channel><title>x</title></channel></rss>', "")
        # A DOCTYPE quoted inside show notes is content, not a prolog.
        parsed = feeds.parse(b'<rss version="2.0"><channel><title>ok</title><item><title>e</title><guid>g</guid>'
                             b'<enclosure url="https://x/1.mp3" type="audio/mpeg"/>'
                             b'<description><![CDATA[<!DOCTYPE html><p>hi</p>]]></description></item></channel></rss>', "")
        self.assertEqual(parsed["podcast"]["title"], "ok")
        self.assertEqual(len(parsed["episodes"]), 1)

    def test_field_and_list_caps(self):
        categories = "".join('<itunes:category text="c%d"/>' % i for i in range(60))
        long_url = "https://x/" + "a" * 3000
        data = ('<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
                'xmlns:podcast="https://podcastindex.org/namespace/1.0"><channel><title>Caps</title>'
                '<itunes:author>%s</itunes:author><language>%s</language>%s'
                '<item><title>e</title><guid>g</guid><enclosure url="%s" type="audio/mpeg"/>'
                '<enclosure url="https://x/ok.mp3" type="%s"/>'
                '%s%s</item></channel></rss>' % (
                    "a" * 1000, "e" * 100, categories, long_url, "t" * 500,
                    "".join('<podcast:transcript url="https://x/t%d" type="text/vtt"/>' % i for i in range(30)),
                    "".join('<podcast:person role="%s">P%d</podcast:person>' % ("r" * 200, i) for i in range(70)),
                )).encode()
        parsed = feeds.parse(data, "")
        podcast = parsed["podcast"]
        self.assertEqual(len(podcast["author"]), feeds.MAX_SMALL)
        self.assertEqual(len(podcast["language"]), 16)
        self.assertEqual(len(podcast["categories"]), feeds.MAX_CATEGORIES)
        ep = parsed["episodes"][0]
        self.assertEqual(ep["enclosure_url"], "https://x/ok.mp3")     # the 3000-char URL was dropped
        self.assertEqual(len(ep["enclosure_type"]), 100)
        self.assertEqual(len(ep["transcripts"]), feeds.MAX_TRANSCRIPTS)
        self.assertEqual(len(ep["persons"]), feeds.MAX_PERSONS)
        self.assertEqual(len(ep["persons"][0]["role"]), 64)
