"""Podcast discovery: Apple's catalogue by default, Podcast Index when the
user has a key.

Both providers return the same result shape so the Discover view does not
care which answered. Apple's Search API is keyless but rate-limited (about
twenty calls a minute, per-country); a token bucket and a short cache keep a
search-as-you-type field under that. Podcast Index needs a free key, is
worldwide, and adds trending shows and search by person.
"""

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
PODCASTINDEX_BASE = "https://api.podcastindex.org/api/1.0/"
CACHE_SECONDS = 15 * 60
TRENDING_CACHE_SECONDS = 60 * 60
ITUNES_PER_MINUTE = 15
MAX_RESULTS = 50


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


def _result(title, author, feed_url, artwork, description="", episode_count=None, itunes_id=None,
            pi_id=None, last_update=None, language="", categories=None):
    return {
        "title": str(title or "").strip(),
        "author": str(author or "").strip(),
        "feedUrl": str(feed_url or "").strip(),
        "artwork": str(artwork or "").strip(),
        "description": str(description or "").strip()[:400],
        "episodeCount": episode_count,
        "itunesId": itunes_id,
        "piId": pi_id,
        "lastUpdate": last_update,
        "language": str(language or "").lower(),
        "categories": categories or [],
        "subscribed": False,
    }


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
            results.append(_result(
                item.get("collectionName"), item.get("artistName"), feed,
                item.get("artworkUrl600") or item.get("artworkUrl100"), "",
                item.get("trackCount"), item.get("collectionId"), None,
                _iso_to_epoch(item.get("releaseDate")), "", [item["primaryGenreName"]] if item.get("primaryGenreName") else []))
        return results

    def trending(self, lang, cat, limit):
        raise Unsupported()


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
    def _feeds(payload):
        results = []
        for item in payload.get("feeds", []) or []:
            feed = item.get("url")
            if not feed:
                continue
            categories = item.get("categories") or {}
            results.append(_result(
                item.get("title"), item.get("author") or item.get("ownerName"), feed,
                item.get("artwork") or item.get("image"), item.get("description"),
                item.get("episodeCount"), item.get("itunesId"), item.get("id"),
                item.get("newestItemPubdate") or item.get("lastUpdateTime"), item.get("language"),
                list(categories.values()) if isinstance(categories, dict) else []))
        return results

    def search(self, query, kind, limit, country, lang):
        endpoint = {"title": "search/bytitle", "person": "search/byperson"}.get(kind, "search/byterm")
        payload = self._get(endpoint, {"q": query, "max": str(limit), "fulltext": "true"})
        if kind == "person":
            # byperson answers episodes; fold them into their feeds.
            seen = {}
            for item in payload.get("items", []) or []:
                feed = item.get("feedUrl")
                if not feed or feed in seen:
                    continue
                seen[feed] = _result(item.get("feedTitle"), item.get("feedAuthor"), feed, item.get("feedImage") or item.get("image"),
                                     "", None, item.get("feedItunesId"), item.get("feedId"), item.get("datePublished"),
                                     item.get("feedLanguage"), [])
            return list(seen.values())
        return self._feeds(payload)

    def trending(self, lang, cat, limit):
        params = {"max": str(limit)}
        if lang:
            params["lang"] = lang
        if cat:
            params["cat"] = cat
        return self._feeds(self._get("podcasts/trending", params))

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


class Search:
    def __init__(self, engine):
        self.engine = engine
        self.itunes = ITunesProvider()
        self._pi = None
        self._pi_key = ("", "")

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
        self.engine.store.execute("DELETE FROM search_cache WHERE fetched_at < ?", (now() - 24 * 3600,))

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
        query = str(query or "").strip()
        if len(query) < 2:
            return {"provider": "", "cached": True, "results": []}
        prov = self.provider_for(provider)
        country = (country or self.engine.settings.country or "US").upper()[:2]
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
