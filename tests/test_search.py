import asyncio
import os
import unittest

from engine import protocol, search
from engine.library import Library
from engine.queue import Queue
from engine.search import Search
from tests.fakes import EngineHarness, FakeHttpServer

HERE = os.path.dirname(os.path.abspath(__file__))


def fixture(name):
    with open(os.path.join(HERE, "fixtures", "feeds", name), "rb") as handle:
        return handle.read()


def attach(engine):
    engine.library = Library(engine)
    engine.queue = Queue(engine)
    engine.search = Search(engine)
    engine.subsystems = [engine.library, engine.queue, engine.search]


def run(coro):
    return asyncio.run(coro)


def rss_entry(pid, name, artist):
    return {
        "im:name": {"label": name}, "im:artist": {"label": artist},
        "im:image": [{"label": "https://img/%s/55.png" % pid}, {"label": "https://img/%s/170.png" % pid}],
        "summary": {"label": "About %s" % name},
        "id": {"label": "https://podcasts.apple.com/us/podcast/id%s" % pid, "attributes": {"im:id": pid}},
    }


class ChartsTest(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttpServer()
        self.feed_url = self.http.add("/feed.xml", fixture("podcasting20.xml"), "application/rss+xml")
        self.calls = {"rss": 0, "lookup": 0, "marketing": 0}
        self.rss_fails = False
        self.marketing_fails = False
        self.rss_empty = False
        self.lookup_empty = False
        server = self

        def rss(method, params, body):
            server.calls["rss"] += 1
            if server.rss_fails:
                raise PermissionError()
            if server.rss_empty:
                return {"feed": {"title": {"label": "empty"}}}
            return {"feed": {"entry": [rss_entry("11", "Alpha", "A"), rss_entry("22", "Beta", "B"), rss_entry("33", "Gamma", "C")]}}

        def lookup(method, params, body):
            server.calls["lookup"] += 1
            if server.lookup_empty:
                return {"resultCount": 0, "results": []}
            ids = params["id"].split(",")
            results = [{"wrapperType": "track", "kind": "podcast", "collectionId": 11, "collectionName": "Alpha", "artistName": "A",
                        "feedUrl": server.feed_url, "artworkUrl600": "https://img/11/600.jpg", "trackCount": 12,
                        "genres": ["Comedy", "Podcasts"], "releaseDate": "2026-09-01T00:00:00Z"},
                       {"collectionId": 33, "collectionName": "Gamma", "artistName": "C", "feedUrl": "https://gamma.example/rss",
                        "artworkUrl600": "https://img/33/600.jpg", "trackCount": 3, "genres": ["News", "Podcasts"]}]
            return {"resultCount": len(results), "results": [r for r in results if str(r["collectionId"]) in ids]}

        def marketing(method, params, body):
            server.calls["marketing"] += 1
            if server.rss_fails and server.marketing_fails:
                raise PermissionError()
            return {"feed": {"results": [{"id": "33", "name": "Gamma", "artistName": "C",
                                          "artworkUrl100": "https://img/33/100x100bb.png"}]}}

        self.http.json_routes["/us/rss/toppodcasts/limit=50/json"] = rss
        self.http.json_routes["/us/rss/toppodcasts/limit=50/genre=1303/json"] = rss
        self.http.json_routes["/lookup"] = lookup
        self.http.json_routes["/us/podcasts/top/50/podcasts.json"] = marketing
        self._saved = (search.ITUNES_CHART_BASE, search.ITUNES_LOOKUP, search.ITUNES_MARKETING_BASE)
        search.ITUNES_CHART_BASE = self.http.base
        search.ITUNES_LOOKUP = self.http.url("/lookup")
        search.ITUNES_MARKETING_BASE = self.http.base

    def tearDown(self):
        search.ITUNES_CHART_BASE, search.ITUNES_LOOKUP, search.ITUNES_MARKETING_BASE = self._saved
        self.http.close()

    def test_chart_order_lookup_and_cache(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                result = await h.engine.search.charts("us", "", 50)
                titles = [r["title"] for r in result["results"]]
                # Beta has no lookup entry and is dropped; ranks follow the chart.
                self.assertEqual(titles, ["Alpha", "Gamma"])
                self.assertEqual([r["rank"] for r in result["results"]], [1, 3])
                self.assertEqual(result["results"][0]["artwork"], "https://img/11/600.jpg")
                self.assertEqual(result["results"][0]["categories"], ["Comedy"])
                self.assertEqual(result["results"][0]["description"], "About Alpha")
                self.assertFalse(result["results"][0]["subscribed"])
                self.assertEqual(self.calls, {"rss": 1, "lookup": 1, "marketing": 0})
                # One lookup carried every id.
                lookup_calls = [r for r in self.http.requests if r[1].startswith("/lookup")]
                self.assertIn("id=11%2C22%2C33", lookup_calls[0][1])
                # Second call is served from the cache and reflects a new subscription.
                await h.engine.library.subscribe(self.feed_url)
                again = await h.engine.search.charts("US", None, 50)
                self.assertTrue(again["cached"])
                self.assertTrue(again["results"][0]["subscribed"])
                self.assertEqual(self.calls["rss"], 1)
                # A genre chart is its own cache entry.
                comedy = await h.engine.search.charts("us", "1303", 50)
                self.assertEqual(comedy["genre"], "1303")
                self.assertEqual(self.calls["rss"], 2)
                with self.assertRaises(protocol.ProtocolError):
                    await h.engine.search.charts("us", "9999", 50)
        run(scenario())

    def test_stale_chart_survives_an_outage(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                first = await h.engine.search.charts("us", "", 50)
                self.assertEqual(len(first["results"]), 2)
                self.rss_fails = True
                self.marketing_fails = True
                refreshed = await h.engine.search.charts("us", "", 50, refresh=True)
                self.assertTrue(refreshed["stale"])
                self.assertEqual(len(refreshed["results"]), 2)
        run(scenario())

    def test_marketing_fallback_without_genre(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                self.rss_fails = True
                result = await h.engine.search.charts("us", "", 50)
                self.assertEqual([r["title"] for r in result["results"]], ["Gamma"])
                self.assertEqual(self.calls["marketing"], 1)
                self.assertEqual(result["results"][0]["rank"], 1)
                with self.assertRaises(protocol.ProtocolError):
                    await h.engine.search.charts("us", "1303", 50)
        run(scenario())

    def test_empty_answers_are_not_cached(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                self.lookup_empty = True
                first = await h.engine.search.charts("us", "", 50)
                self.assertEqual(first["results"], [])
                self.lookup_empty = False
                second = await h.engine.search.charts("us", "", 50)
                self.assertFalse(second["cached"])
                self.assertEqual(len(second["results"]), 2)
                # An empty old-style chart falls through to the marketing feed.
                self.rss_empty = True
                third = await h.engine.search.charts("us", "", 50, refresh=True)
                self.assertEqual([r["title"] for r in third["results"]], ["Gamma"])
                self.assertEqual(self.calls["marketing"], 1)
                # ...and the refresh that found only the fallback did not erase
                # the better chart: a later outage still serves two rows? No —
                # the fallback chart is a real chart and replaces it.
                self.assertEqual(len(h.engine.store.all("SELECT key FROM search_cache WHERE key LIKE 'charts|%'")), 1)
        run(scenario())

    def test_bad_country_is_named(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                h.engine.settings.update({"searchCountry": "United States"})
                with self.assertRaises(protocol.ProtocolError) as caught:
                    await h.engine.search.charts(None, "", 50)
                self.assertEqual(caught.exception.code, protocol.BAD_REQUEST)
                self.assertEqual(self.calls["rss"], 0)
        run(scenario())

    def test_concurrent_requests_share_one_fetch(self):
        async def scenario():
            async with EngineHarness(attach) as h:
                results = await asyncio.gather(*(h.engine.search.charts("us", "", 50) for _ in range(3)))
                self.assertTrue(all(len(r["results"]) == 2 for r in results))
                self.assertEqual(self.calls, {"rss": 1, "lookup": 1, "marketing": 0})
        run(scenario())

    def test_genres_command(self):
        genres = search.cmd_genres(None, None)["genres"]
        self.assertEqual(genres[0], {"id": "1303", "name": "Comedy"})
        self.assertEqual(len(genres), 19)


if __name__ == "__main__":
    unittest.main()
