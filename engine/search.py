"""Podcast discovery: Apple's catalogue by default, Podcast Index when the
user has a key.

Both providers return the same result shape so the Browse view does not
care which answered. Apple's Search API is keyless but rate-limited (about
twenty calls a minute, per-country); a token bucket and a short cache keep a
search-as-you-type field under that. Apple's top charts (per storefront,
optionally per genre) are keyless too: the chart feed lists ids, one lookup
call turns them into feeds. Podcast Index needs a free key, is worldwide, and
adds trending shows and search by person.
"""

import asyncio
import datetime
import hashlib
import json
import time
import urllib.parse

from . import http, log, protocol
from .store import now

LOG = log.get("search")
A = protocol.Arg

ITUNES_SEARCH = "https://itunes.apple.com/search"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
# {cc}/rss/toppodcasts/limit=N[/genre=ID]/json — the old RSS generator still
# answers and is the only keyless chart that knows about genres.
ITUNES_CHART_BASE = "https://itunes.apple.com"
# Genre-less fallback when a storefront has no old-style chart.
ITUNES_MARKETING_BASE = "https://rss.marketingtools.apple.com/api/v2"
PODCASTINDEX_BASE = "https://api.podcastindex.org/api/1.0/"
CACHE_SECONDS = 15 * 60
TRENDING_CACHE_SECONDS = 60 * 60
CHARTS_CACHE_SECONDS = 6 * 3600
STALE_CACHE_SECONDS = 7 * 86400
# With a cached chart in hand, Apple gets this long before the cached one is
# shown and the fetch finishes in the background.
STALE_GRACE_SECONDS = 3
ITUNES_PER_MINUTE = 15
MAX_RESULTS = 50
CHART_LIMIT = 50

# Apple's top-level podcast genres (MZStoreServices genre tree, id 26), in
# the order the Browse view offers them.
GENRES = [
    ("1303", "Comedy"), ("1489", "News"), ("1488", "True Crime"), ("1324", "Society & Culture"),
    ("1321", "Business"), ("1304", "Education"), ("1318", "Technology"), ("1512", "Health & Fitness"),
    ("1545", "Sports"), ("1487", "History"), ("1533", "Science"), ("1301", "Arts"), ("1309", "TV & Film"),
    ("1310", "Music"), ("1483", "Fiction"), ("1305", "Kids & Family"), ("1502", "Leisure"),
    ("1314", "Religion & Spirituality"), ("1511", "Government"),
]
GENRE_IDS = {gid for gid, _name in GENRES}


class Unsupported(Exception):
    pass


class TokenBucket:
    def __init__(self, per_minute):
        self.capacity = per_minute
        self.tokens = float(per_minute)
        self.updated = time.monotonic()

    def take(self):
        stamp = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (stamp - self.updated) * self.capacity / 60.0)
        self.updated = stamp
        if self.tokens >= 1:
            self.tokens -= 1
            return 0
        return int((1 - self.tokens) * 60.0 / self.capacity) + 1


def canonical_feed_url(url):
    """Lowercase host, no trailing slash, http/https twins fold together."""
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return str(url or "").strip()
    if not parts.netloc:
        return str(url or "").strip()
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(("https", parts.netloc.lower(), path, parts.query, ""))


MAX_URL = 2048
MAX_TEXT = 400
MAX_QUERY = 200


def _url(value):
    """A catalogue-supplied URL the daemon would actually fetch, else ""."""
    text = str(value or "").strip()
    if not text or len(text) > MAX_URL:
        return ""
    try:
        return http.check_url(text)
    except http.FetchError:
        return ""


def _result(title, author, feed_url, artwork, description="", episode_count=None, itunes_id=None,
            pi_id=None, last_update=None, language="", categories=None):
    """One search result, or None when the catalogue's feed URL is not one
    we could subscribe to. Every string is bounded; the artwork is only
    ever a URL the daemon fetches and re-encodes itself (see artwork-thumb),
    never something the shell loads directly."""
    feed_url = _url(feed_url)
    if not feed_url:
        return None
    return {
        "title": str(title or "").strip()[:MAX_TEXT],
        "author": str(author or "").strip()[:MAX_TEXT],
        "feedUrl": feed_url,
        "artwork": _url(artwork),
        "description": str(description or "").strip()[:MAX_TEXT],
        "episodeCount": episode_count if isinstance(episode_count, int) and not isinstance(episode_count, bool) else None,
        "itunesId": itunes_id if isinstance(itunes_id, (int, str)) else None,
        "piId": pi_id if isinstance(pi_id, (int, str)) else None,
        "lastUpdate": last_update if isinstance(last_update, (int, float)) and not isinstance(last_update, bool) else None,
        "language": str(language or "").lower()[:16],
        "categories": [str(c)[:100] for c in (categories or [])[:10] if isinstance(c, str)],
        "subscribed": False,
    }


def _append(results, result):
    if result is not None:
        results.append(result)


class ITunesProvider:
    name = "itunes"

    def __init__(self):
        self.bucket = TokenBucket(ITUNES_PER_MINUTE)

    def search(self, query, kind, limit, country, lang):
        wait = self.bucket.take()
        if wait:
            raise protocol.ProtocolError(protocol.RATE_LIMITED, "Apple's catalogue is being asked too often; wait a moment", retryAfter=wait)
        params = {"media": "podcast", "entity": "podcast", "term": query, "limit": str(limit), "country": country or "US"}
        if kind == "title":
            params["attribute"] = "titleTerm"
        elif kind == "person":
            params["attribute"] = "authorTerm"
        url = ITUNES_SEARCH + "?" + urllib.parse.urlencode(params)
        response = http.fetch(url, cap=http.SMALL_CAP, timeout=15, accept="application/json")
        try:
            payload = json.loads(response.body.decode("utf-8", "replace"))
        except ValueError:
            raise protocol.ProtocolError(protocol.NETWORK, "Apple's catalogue answered with something that is not JSON")
        results = []
        for item in payload.get("results", []):
            feed = item.get("feedUrl")
            if not feed:
                continue
            _append(results, _result(
                item.get("collectionName"), item.get("artistName"), feed,
                item.get("artworkUrl600") or item.get("artworkUrl100"), "",
                item.get("trackCount"), item.get("collectionId"), None,
                _iso_to_epoch(item.get("releaseDate")), "", [item["primaryGenreName"]] if item.get("primaryGenreName") else []))
        return results[:limit]

    def trending(self, lang, cat, limit):
        raise Unsupported()

    # ---- charts ------------------------------------------------------------

    def charts(self, country, genre, limit):
        """Top podcasts for a storefront, optionally within a genre, in chart
        order with a `rank` on each result."""
        cc = (country or "us").lower()[:2]
        wait = self.bucket.take()
        if wait:
            raise protocol.ProtocolError(protocol.RATE_LIMITED, "Apple's catalogue is being asked too often; wait a moment", retryAfter=wait)
        entries = self._chart_entries(cc, genre, limit)
        if not entries:
            return []
        params = {"id": ",".join(entry["id"] for entry in entries), "entity": "podcast", "country": cc}
        payload = self._json(ITUNES_LOOKUP + "?" + urllib.parse.urlencode(params))
        by_id = {}
        for item in payload.get("results", []) or []:
            if isinstance(item, dict) and item.get("collectionId") is not None:
                by_id[str(item["collectionId"])] = item
        results = []
        for rank, entry in enumerate(entries, 1):
            item = by_id.get(entry["id"])
            if item is None or not item.get("feedUrl"):
                continue
            genres = [g for g in (item.get("genres") or []) if isinstance(g, str) and g != "Podcasts"]
            result = _result(
                item.get("collectionName") or entry["name"], item.get("artistName") or entry["artist"], item["feedUrl"],
                item.get("artworkUrl600") or entry["artwork"], entry["summary"], item.get("trackCount"),
                item.get("collectionId"), None, _iso_to_epoch(item.get("releaseDate")), "", genres[:3])
            if result is None:
                continue
            result["rank"] = rank
            results.append(result)
        return results[:limit]

    def _chart_entries(self, cc, genre, limit):
        try:
            entries = self._chart_rss(cc, genre, limit)
        except (http.FetchError, protocol.ProtocolError) as error:
            if genre:
                raise
            LOG.info("old-style chart for %s unavailable (%s); trying the marketing feed", cc, getattr(error, "message", error))
            return self._chart_marketing(cc, limit)
        if not entries and not genre:
            LOG.info("old-style chart for %s is empty; trying the marketing feed", cc)
            return self._chart_marketing(cc, limit)
        return entries

    def _chart_rss(self, cc, genre, limit):
        path = "/%s/rss/toppodcasts/limit=%d%s/json" % (cc, limit, ("/genre=%s" % genre) if genre else "")
        payload = self._json(ITUNES_CHART_BASE + path)
        feed = payload.get("feed") if isinstance(payload, dict) else None
        entries = (feed or {}).get("entry") or []
        if isinstance(entries, dict):          # a one-item chart comes as an object
            entries = [entries]
        parsed = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            pid = str(_label(entry.get("id"), "attributes", "im:id") or "").strip()
            if not pid.isdigit():
                continue
            images = entry.get("im:image") or []
            artwork = _label(images[-1]) if isinstance(images, list) and images else ""
            parsed.append({
                "id": pid, "name": _label(entry.get("im:name")), "artist": _label(entry.get("im:artist")),
                "artwork": str(artwork or ""), "summary": _label(entry.get("summary")),
            })
        return parsed

    def _chart_marketing(self, cc, limit):
        payload = self._json(ITUNES_MARKETING_BASE + "/%s/podcasts/top/%d/podcasts.json" % (cc, limit))
        feed = payload.get("feed") if isinstance(payload, dict) else None
        parsed = []
        for item in (feed or {}).get("results") or []:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "").strip()
            if not pid.isdigit():
                continue
            artwork = str(item.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
            parsed.append({"id": pid, "name": item.get("name"), "artist": item.get("artistName"), "artwork": artwork, "summary": ""})
        return parsed

    @staticmethod
    def _json(url):
        response = http.fetch(url, cap=http.SMALL_CAP, timeout=20, accept="application/json")
        try:
            payload = json.loads(response.body.decode("utf-8", "replace"))
        except ValueError:
            raise protocol.ProtocolError(protocol.NETWORK, "Apple's catalogue answered with something that is not JSON")
        return payload if isinstance(payload, dict) else {}


def _label(node, *path):
    """Apple's RSS-as-JSON wraps every value: {"label": "..."} or nested
    {"attributes": {...}}. Returns "" for anything missing."""
    value = node
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    if isinstance(value, dict):
        value = value.get("label")
    return str(value or "").strip() if not isinstance(value, dict) else ""


class PodcastIndexProvider:
    name = "podcastindex"

    def __init__(self, key, secret):
        self.key = key
        self.secret = secret

    def _headers(self):
        stamp = str(int(time.time()))
        digest = hashlib.sha1((self.key + self.secret + stamp).encode("utf-8")).hexdigest()
        return {"X-Auth-Key": self.key, "X-Auth-Date": stamp, "Authorization": digest}

    def _get(self, endpoint, params):
        url = PODCASTINDEX_BASE + endpoint + "?" + urllib.parse.urlencode(params)
        try:
            response = http.fetch(url, cap=http.SMALL_CAP, timeout=15, accept="application/json", extra_headers=self._headers())
        except http.FetchError as error:
            if error.status in (401, 403):
                raise protocol.ProtocolError(protocol.BAD_REQUEST, "Podcast Index rejected the API key; check it in Settings")
            raise
        try:
            return json.loads(response.body.decode("utf-8", "replace"))
        except ValueError:
            raise protocol.ProtocolError(protocol.NETWORK, "Podcast Index answered with something that is not JSON")

    @staticmethod
    def _feeds(payload, limit=None):
        results = []
        for item in payload.get("feeds", []) or []:
            if not isinstance(item, dict):
                continue
            feed = item.get("url")
            if not feed:
                continue
            categories = item.get("categories") or {}
            _append(results, _result(
                item.get("title"), item.get("author") or item.get("ownerName"), feed,
                item.get("artwork") or item.get("image"), item.get("description"),
                item.get("episodeCount"), item.get("itunesId"), item.get("id"),
                item.get("newestItemPubdate") or item.get("lastUpdateTime"), item.get("language"),
                list(categories.values()) if isinstance(categories, dict) else []))
            if limit is not None and len(results) >= limit:
                break
        return results

    def search(self, query, kind, limit, country, lang):
        endpoint = {"title": "search/bytitle", "person": "search/byperson"}.get(kind, "search/byterm")
        payload = self._get(endpoint, {"q": query, "max": str(limit), "fulltext": "true"})
        if kind == "person":
            # byperson answers episodes; fold them into their feeds.
            seen = {}
            for item in payload.get("items", []) or []:
                if not isinstance(item, dict):
                    continue
                feed = item.get("feedUrl")
                if not feed or feed in seen:
                    continue
                result = _result(item.get("feedTitle"), item.get("feedAuthor"), feed, item.get("feedImage") or item.get("image"),
                                 "", None, item.get("feedItunesId"), item.get("feedId"), item.get("datePublished"),
                                 item.get("feedLanguage"), [])
                if result is not None:
                    seen[feed] = result
                if len(seen) >= limit:
                    break
            return list(seen.values())
        return self._feeds(payload, limit)

    def trending(self, lang, cat, limit):
        params = {"max": str(limit)}
        if lang:
            params["lang"] = lang
        if cat:
            params["cat"] = cat
        return self._feeds(self._get("podcasts/trending", params), limit)

    def categories(self):
        payload = self._get("categories/list", {})
        return [{"id": item.get("id"), "name": item.get("name")} for item in payload.get("feeds", []) or []]


def _iso_to_epoch(value):
    if not value:
        return None
    try:
        return int(datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def storefront(country, configured):
    """The two-letter Apple storefront to ask, or a BAD_REQUEST that names the
    setting: Apple answers 400/500 for an unknown code, which is no help."""
    code = str(country or configured or "US").strip()
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        raise protocol.ProtocolError(protocol.BAD_REQUEST, "%r is not a two-letter country code; set searchCountry to one (or auto)" % code)
    return code.upper()


class Search:
    def __init__(self, engine):
        self.engine = engine
        self.itunes = ITunesProvider()
        self._pi = None
        self._pi_key = ("", "")
        self._inflight = {}          # chart cache key -> task, so bursts share one fetch

    async def start(self):
        self.engine.on_settings_changed(self._refresh_providers)
        self._refresh_providers()

    async def stop(self, restart=False, quit_mpv=True):
        pass

    def _refresh_providers(self):
        key = self.engine.credential("podcastindex", "key")
        secret = self.engine.credential("podcastindex", "secret")
        if key and secret:
            if (key, secret) != self._pi_key or self._pi is None:
                self._pi = PodcastIndexProvider(key, secret)
                self._pi_key = (key, secret)
        else:
            self._pi = None
            self._pi_key = ("", "")
        providers = dict(self.engine.state["engine"].get("providers") or {})
        providers["itunes"] = True
        providers["podcastindex"] = self._pi is not None
        self.engine.update_state("engine", providers=providers)

    def provider_for(self, name):
        wanted = name or self.engine.settings.searchProvider
        if wanted == "podcastindex":
            if self._pi is not None:
                return self._pi
            self.engine.notice("warn", "Podcast Index needs an API key (Settings); using Apple's catalogue instead", code="pi-key-missing")
        return self.itunes

    # ---- cache -------------------------------------------------------------

    def _cache_get(self, key, ttl):
        row = self.engine.store.one("SELECT response_json, fetched_at FROM search_cache WHERE key = ?", (key,))
        if row is None or now() - int(row["fetched_at"]) > ttl:
            return None
        try:
            return json.loads(row["response_json"])
        except ValueError:
            return None

    def _cache_put(self, key, results):
        self.engine.store.execute(
            "INSERT INTO search_cache (key, response_json, fetched_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET response_json = excluded.response_json, fetched_at = excluded.fetched_at",
            (key, json.dumps(results), now()))

    def prune_cache(self):
        # Charts stay a week as a fallback for offline days; searches a day.
        self.engine.store.execute("DELETE FROM search_cache WHERE key LIKE 'charts|%' AND fetched_at < ?", (now() - STALE_CACHE_SECONDS,))
        self.engine.store.execute("DELETE FROM search_cache WHERE key NOT LIKE 'charts|%' AND fetched_at < ?", (now() - 24 * 3600,))

    # ---- public ------------------------------------------------------------

    def _mark_subscribed(self, results):
        known = {canonical_feed_url(row["feed_url"]): row["id"] for row in self.engine.store.all("SELECT id, feed_url FROM podcasts")}
        deduped = []
        seen = set()
        for item in results:
            key = canonical_feed_url(item["feedUrl"])
            if key in seen:
                continue
            seen.add(key)
            item["subscribed"] = key in known
            item["podcastId"] = known.get(key)
            deduped.append(item)
        return deduped

    async def search(self, query, kind="term", provider=None, country=None, lang=None, limit=25):
        query = str(query or "").strip()[:MAX_QUERY]
        if len(query) < 2:
            return {"provider": "", "cached": True, "results": []}
        prov = self.provider_for(provider)
        country = storefront(country, self.engine.settings.country)
        lang = lang or ""
        limit = max(1, min(MAX_RESULTS, int(limit)))
        cache_key = "|".join([prov.name, kind, query.lower(), country, lang, str(limit)])
        cached = self._cache_get(cache_key, CACHE_SECONDS)
        if cached is not None:
            return {"provider": prov.name, "cached": True, "results": self._mark_subscribed(cached)}
        try:
            results = await self.engine.run_in_thread(prov.search, query, kind, limit, country, lang)
        except http.FetchError as error:
            raise protocol.ProtocolError(protocol.NETWORK, error.message)
        self._cache_put(cache_key, results)
        return {"provider": prov.name, "cached": False, "results": self._mark_subscribed(results)}

    async def charts(self, country=None, genre=None, limit=CHART_LIMIT, refresh=False):
        country = storefront(country, self.engine.settings.country)
        genre = str(genre or "")
        if genre and genre not in GENRE_IDS:
            raise protocol.ProtocolError(protocol.BAD_REQUEST, "unknown genre %r" % genre)
        limit = max(1, min(CHART_LIMIT, int(limit)))
        cache_key = "|".join(["charts", country, genre, str(limit)])

        def reply(results, cached, stale, **extra):
            answer = {"provider": "itunes", "country": country, "genre": genre, "cached": cached, "stale": stale,
                      "results": self._mark_subscribed(results)}
            answer.update(extra)
            return answer

        # An empty list is never a chart worth keeping: a transient empty
        # answer must not freeze the page for six hours.
        cached = None if refresh else (self._cache_get(cache_key, CHARTS_CACHE_SECONDS) or None)
        if cached is not None:
            return reply(cached, True, False)
        # A chart that is a few hours old beats an empty page, and beats a
        # spinner: with one in hand Apple only gets a short grace period.
        stale = self._cache_get(cache_key, STALE_CACHE_SECONDS) or None
        task = self._inflight.get(cache_key)
        if task is None:
            task = self._inflight[cache_key] = asyncio.ensure_future(self._fetch_chart(cache_key, country, genre, limit))
        if stale is not None:
            done, _ = await asyncio.wait({task}, timeout=STALE_GRACE_SECONDS)
            if not done:
                LOG.warning("charts %s/%s: Apple is slow; serving the cached chart", country, genre or "top")
                return reply(stale, True, True, reason="slow")
        try:
            results = await task
        except (http.FetchError, protocol.ProtocolError) as error:
            if stale is not None:
                LOG.warning("charts %s/%s: serving the cached chart (%s)", country, genre or "top", getattr(error, "message", error))
                extra = {}
                if isinstance(error, protocol.ProtocolError) and error.code == protocol.RATE_LIMITED:
                    extra = {"reason": "rate-limited", "retryAfter": error.extra.get("retryAfter")}
                else:
                    extra = {"reason": "unreachable"}
                return reply(stale, True, True, **extra)
            if isinstance(error, http.FetchError):
                hint = "; is %s a real Apple storefront?" % country if error.status in (400, 404, 500) else ""
                raise protocol.ProtocolError(protocol.NETWORK, error.message + hint)
            raise
        return reply(results, False, False)

    async def _fetch_chart(self, cache_key, country, genre, limit):
        try:
            results = await self.engine.run_in_thread(self.itunes.charts, country, genre, limit)
            if results:
                self._cache_put(cache_key, results)
            return results
        finally:
            self._inflight.pop(cache_key, None)

    async def trending(self, lang=None, cat=None, limit=25):
        if self._pi is None:
            raise protocol.ProtocolError(protocol.UNAVAILABLE, "Trending needs a Podcast Index API key", reason="pi-key-missing")
        lang = lang if lang is not None else self.engine.settings.language
        limit = max(1, min(MAX_RESULTS, int(limit)))
        cache_key = "|".join(["pi-trending", lang or "", cat or "", str(limit)])
        cached = self._cache_get(cache_key, TRENDING_CACHE_SECONDS)
        if cached is not None:
            return {"provider": "podcastindex", "cached": True, "results": self._mark_subscribed(cached)}
        try:
            results = await self.engine.run_in_thread(self._pi.trending, lang, cat, limit)
        except http.FetchError as error:
            raise protocol.ProtocolError(protocol.NETWORK, error.message)
        self._cache_put(cache_key, results)
        return {"provider": "podcastindex", "cached": False, "results": self._mark_subscribed(results)}


# ---------------------------------------------------------------- commands

@protocol.command("search", "Search the catalogue", query=A(str),
                  kind=A(str, required=False, default="term", choices=["term", "title", "person"]),
                  provider=A(str, required=False, choices=["itunes", "podcastindex"]),
                  country=A(str, required=False), lang=A(str, required=False),
                  limit=A(int, required=False, default=25, minimum=1, maximum=MAX_RESULTS))
async def cmd_search(engine, client, query, kind, provider, country, lang, limit):
    return await engine.search.search(query, kind, provider, country, lang, limit)


@protocol.command("trending", "Trending shows (Podcast Index)", lang=A(str, required=False), cat=A(str, required=False),
                  limit=A(int, required=False, default=25, minimum=1, maximum=MAX_RESULTS))
async def cmd_trending(engine, client, lang, cat, limit):
    return await engine.search.trending(lang, cat, limit)


@protocol.command("charts", "Top podcasts for a region, optionally within a genre (Apple)",
                  country=A(str, required=False), genre=A(str, required=False),
                  limit=A(int, required=False, default=CHART_LIMIT, minimum=1, maximum=CHART_LIMIT),
                  refresh=A(bool, required=False, default=False))
async def cmd_charts(engine, client, country, genre, limit, refresh):
    return await engine.search.charts(country, genre, limit, refresh)


@protocol.command("genres", "The chart genres Browse can ask for")
def cmd_genres(engine, client):
    return {"genres": [{"id": gid, "name": name} for gid, name in GENRES]}
