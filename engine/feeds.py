"""RSS parsing for podcast feeds.

Streams the document with ElementTree.iterparse so a 40 MB feed with three
thousand items does not become forty megabytes of tree; each item is turned
into a plain dict and cleared. Namespaces are matched by URI with a
local-name fallback, because feeds misspell the podcast namespace URI in
every way imaginable. Everything that can be missing is optional, and every
failure to parse one field degrades that field, never the item.

`parse(data, feed_url)` -> {"podcast": {...}, "episodes": [...]}.
"""

import datetime
import email.utils
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET

from . import htmlclean, log

LOG = log.get("feeds")

MAX_ITEMS = 500
# Feeds are parsed with items cleared as they go; everything else stays in
# the tree, so a document that is nothing but elements is capped outright.
MAX_ELEMENTS_OUTSIDE_ITEMS = 100000
MAX_TITLE = 400
MAX_TEXT_FIELD = 2000

NS_ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
NS_PODCAST = {
    "https://podcastindex.org/namespace/1.0",
    "https://github.com/podcastindex-org/podcast-namespace/blob/main/docs/1.0.md",
}
NS_CONTENT = "http://purl.org/rss/1.0/modules/content/"
NS_MEDIA = "http://search.yahoo.com/mrss/"
NS_ATOM = "http://www.w3.org/2005/atom"
NS_DC = "http://purl.org/dc/elements/1.1/"

AUDIO_TYPES = ("audio/",)
VIDEO_TYPES = ("video/",)


class FeedParseError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def _split(tag):
    """'{uri}name' -> (uri-lowercase, name-lowercase)."""
    if tag and tag[0] == "{":
        uri, _, name = tag[1:].partition("}")
        return uri.lower(), name.lower()
    return "", str(tag or "").lower()


def _kind(elem):
    """Classify an element by namespace family: itunes / podcast / content /
    media / atom / dc / plain, plus its local name."""
    uri, name = _split(elem.tag)
    if uri == NS_ITUNES or uri.endswith("itunes.com/dtds/podcast-1.0.dtd"):
        return "itunes", name
    if uri in NS_PODCAST or "podcastindex" in uri or "podcast-namespace" in uri:
        return "podcast", name
    if uri == NS_CONTENT:
        return "content", name
    if uri == NS_MEDIA:
        return "media", name
    if uri == NS_ATOM:
        return "atom", name
    if uri == NS_DC:
        return "dc", name
    if uri == "":
        return "plain", name
    # Unknown prefix with a well-known local name: trust the local name.
    return "other", name


def _text(elem):
    if elem is None:
        return ""
    return (elem.text or "").strip()


def _attr(elem, *names):
    for name in names:
        for key, value in elem.attrib.items():
            if _split(key)[1] == name and value is not None:
                return value.strip()
    return ""


MAX_DURATION = 100 * 3600


def parse_duration(raw):
    """'1:02:03' -> 3723, '45:10' -> 2710, '3600' -> 3600, '12.5' -> 12.
    Anything absurd (more than 100 hours, or numbers too long to be a time)
    is treated as unknown rather than trusted."""
    text = str(raw or "").strip()
    if not text or len(text) > 32:
        return None
    try:
        if re.fullmatch(r"\d{1,9}(\.\d+)?", text):
            value = int(float(text))
            return value if 0 <= value <= MAX_DURATION else None
        match = re.fullmatch(r"(?:(\d{1,3}):)?(\d{1,2}):(\d{1,2})(?:\.\d+)?", text)
        if match:
            value = int(match.group(1) or 0) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
            return value if value <= MAX_DURATION else None
        match = re.search(r"(\d{1,3})\s*(?:h|hr|hours?)", text, re.I)
        match2 = re.search(r"(\d{1,4})\s*(?:m|min|minutes?)", text, re.I)
        if match or match2:
            value = (int(match.group(1)) * 3600 if match else 0) + (int(match2.group(1)) * 60 if match2 else 0)
            return value if value <= MAX_DURATION else None
    except (ValueError, OverflowError):
        return None
    return None


def parse_date(raw):
    """RFC 2822 first, then ISO 8601, then a couple of common mistakes.
    Returns a unix timestamp or None."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(text)
        if parsed is not None:
            return _to_epoch(parsed)
    except (TypeError, ValueError, IndexError):
        pass
    cleaned = re.sub(r"\s+(UTC|GMT|Z)$", "+00:00", text).replace("Z", "+00:00")
    try:
        return _to_epoch(datetime.datetime.fromisoformat(cleaned))
    except ValueError:
        pass
    # "Tue, 03 Jan 2023 10:00:00 GMT+0000" / wrong weekday / missing seconds.
    match = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", text)
    if match:
        months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
        month = match.group(2).lower()
        if month in months:
            try:
                parsed = datetime.datetime(int(match.group(3)), months.index(month) + 1, int(match.group(1)),
                                           int(match.group(4) or 0), int(match.group(5) or 0), int(match.group(6) or 0),
                                           tzinfo=datetime.timezone.utc)
                return _to_epoch(parsed)
            except ValueError:
                return None
    return None


def _to_epoch(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    try:
        stamp = int(value.timestamp())
    except (OverflowError, ValueError, OSError):
        return None
    # Before 1980 or after 2200 is a broken clock, not a publication date.
    return stamp if 315532800 <= stamp <= 7258118400 else None


def _http_url(value):
    text = str(value or "").strip()
    return text if text.lower().startswith(("http://", "https://")) else ""


def _int(value):
    text = str(value).strip()
    if not re.fullmatch(r"-?\d{1,15}", text):
        return None
    return int(text)


def _clip(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit]


def _explicit(value):
    return str(value or "").strip().lower() in ("yes", "true", "explicit", "1")


# ---------------------------------------------------------------- parsing

def decode(data):
    """Bytes -> an ElementTree-parsable byte string, surviving BOMs, wrong
    declarations and stray control characters."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16").encode("utf-8")
        except UnicodeDecodeError:
            pass
    return data.lstrip()


def _recover(data):
    """Second attempt after a ParseError: decode with the declared (or utf-8)
    codec replacing junk, strip control characters, drop the declaration."""
    match = re.match(rb"\s*<\?xml[^>]*encoding=[\"']([A-Za-z0-9._-]+)[\"']", data)
    codec = match.group(1).decode("ascii", "ignore") if match else "utf-8"
    try:
        text = data.decode(codec, "replace")
    except LookupError:
        text = data.decode("utf-8", "replace")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"&(?!(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
    return text.encode("utf-8")


def parse(data, feed_url="", max_items=MAX_ITEMS):
    payload = decode(data)
    try:
        return _parse_bytes(payload, feed_url, max_items)
    except ET.ParseError as first:
        try:
            return _parse_bytes(_recover(payload), feed_url, max_items)
        except ET.ParseError as second:
            raise FeedParseError("not a valid feed: %s" % (second or first))


def _parse_bytes(payload, feed_url, max_items):
    stream = io.BytesIO(payload)
    iterator = ET.iterparse(stream, events=("start", "end"))
    root = None
    channel = None
    episodes = []
    seen_guids = set()
    item_names = ("item", "entry")
    depth = 0
    item_depth = None
    is_atom = False

    outside = 0
    for event, elem in iterator:
        family, name = _kind(elem)
        if event == "start":
            depth += 1
            if item_depth is None:
                outside += 1
                if outside > MAX_ELEMENTS_OUTSIDE_ITEMS:
                    raise FeedParseError("the feed has far too many elements to be a podcast feed")
            if root is None:
                root = elem
                is_atom = name == "feed"
            if name in item_names and item_depth is None and (family in ("plain", "atom", "other")):
                item_depth = depth
            if name == "channel" and channel is None:
                channel = elem
            continue

        # end event
        if item_depth is not None and depth == item_depth and name in item_names:
            if len(episodes) < max_items:
                episode = _parse_item(elem, is_atom)
                if episode is not None and episode["guid"] not in seen_guids:
                    seen_guids.add(episode["guid"])
                    episodes.append(episode)
            item_depth = None
            elem.clear()
        depth -= 1
        if len(episodes) >= max_items and item_depth is None:
            # Enough items; the channel metadata sits above the items in every
            # real feed, so it has been seen by now.
            break

    if root is None:
        raise ET.ParseError("empty document")
    if is_atom:
        channel = root
    if channel is None:
        channel = root.find("channel")
    if channel is None:
        raise ET.ParseError("no <channel> element")
    podcast = _parse_channel(channel, is_atom)
    podcast["feed_url"] = feed_url
    return {"podcast": podcast, "episodes": episodes}


def _parse_channel(channel, is_atom):
    info = {
        "title": "", "author": "", "description_html": "", "description_text": "", "link": "",
        "language": "", "image_url": "", "podcast_guid": None, "funding": [], "itunes_type": "",
        "explicit": False, "categories": [],
    }
    summary = ""
    description = ""
    for elem in list(channel):
        family, name = _kind(elem)
        if name in ("item", "entry"):
            continue
        if name == "title" and not info["title"]:
            info["title"] = _clip(_text(elem), MAX_TITLE)
        elif name == "link" and family in ("plain", "other"):
            info["link"] = _http_url(_text(elem)) or info["link"]
        elif name == "link" and family == "atom":
            rel = _attr(elem, "rel") or "alternate"
            if rel == "alternate" and not info["link"]:
                info["link"] = _http_url(_attr(elem, "href"))
        elif name == "description" and family in ("plain", "other"):
            description = _text(elem) or description
        elif name == "subtitle" and family == "atom":
            description = description or _text(elem)
        elif name == "summary" and family == "itunes":
            summary = _text(elem)
        elif name == "author" and family == "itunes":
            info["author"] = info["author"] or _clip(_text(elem), MAX_TITLE)
        elif name == "author" and family == "atom":
            child = None
            for sub in elem:
                if _kind(sub)[1] == "name":
                    child = sub
            info["author"] = info["author"] or _text(child)
        elif name == "owner" and family == "itunes" and not info["author"]:
            for sub in elem:
                if _kind(sub)[1] == "name":
                    info["author"] = _text(sub)
        elif name == "creator" and family == "dc":
            info["author"] = info["author"] or _text(elem)
        elif name == "language":
            info["language"] = _text(elem).lower()
        elif name == "image" and family == "itunes":
            info["image_url"] = _http_url(_attr(elem, "href")) or info["image_url"]
        elif name == "image" and family in ("plain", "other"):
            for sub in elem:
                if _kind(sub)[1] == "url":
                    info["image_url"] = info["image_url"] or _http_url(_text(sub))
        elif name in ("logo", "icon") and family == "atom":
            info["image_url"] = info["image_url"] or _http_url(_text(elem))
        elif name == "guid" and family == "podcast":
            info["podcast_guid"] = _text(elem) or None
        elif name == "funding" and family == "podcast":
            url = _http_url(_attr(elem, "url"))
            if url:
                info["funding"].append({"url": url, "text": _text(elem)})
        elif name == "type" and family == "itunes":
            info["itunes_type"] = _text(elem).lower()
        elif name == "explicit" and family == "itunes":
            info["explicit"] = _explicit(_text(elem))
        elif name == "category" and family == "itunes":
            label = _attr(elem, "text")
            if label and label not in info["categories"]:
                info["categories"].append(label)
    rich, text = htmlclean.clean(description or summary)
    info["description_html"] = rich
    info["description_text"] = text
    return info


def _parse_item(item, is_atom):
    ep = {
        "guid": "", "title": "", "link": "", "pub_date": None, "duration": None,
        "enclosure_url": "", "enclosure_type": "", "enclosure_length": None,
        "notes_html": "", "notes_text": "", "image_url": "",
        "episode_number": None, "season": None, "episode_type": "full", "explicit": False,
        "chapters_url": "", "chapters_type": "", "transcripts": [], "persons": [],
    }
    description = ""
    encoded = ""
    summary = ""
    enclosures = []
    guid = ""
    for elem in list(item):
        family, name = _kind(elem)
        if name == "title" and not ep["title"]:
            ep["title"] = _clip(_text(elem), MAX_TITLE)
        elif name == "guid" or (name == "id" and family == "atom"):
            guid = _text(elem)
        elif name == "link" and family == "atom":
            rel = _attr(elem, "rel") or "alternate"
            href = _http_url(_attr(elem, "href"))
            if rel == "enclosure" and href:
                enclosures.append((href, _attr(elem, "type").lower(), _int(_attr(elem, "length"))))
            elif rel == "alternate" and href and not ep["link"]:
                ep["link"] = href
        elif name == "link" and family in ("plain", "other"):
            ep["link"] = _http_url(_text(elem)) or ep["link"]
        elif name in ("pubdate", "published", "updated", "date") and ep["pub_date"] is None:
            ep["pub_date"] = parse_date(_text(elem))
        elif name == "enclosure":
            url = _http_url(_attr(elem, "url"))
            if url:
                enclosures.append((url, _attr(elem, "type").lower(), _int(_attr(elem, "length"))))
        elif name == "content" and family == "media":
            url = _http_url(_attr(elem, "url"))
            medium = _attr(elem, "medium").lower()
            mime = _attr(elem, "type").lower()
            if url and (mime.startswith(AUDIO_TYPES) or medium == "audio" or mime.startswith(VIDEO_TYPES) or medium == "video"):
                enclosures.append((url, mime or ("audio/mpeg" if medium == "audio" else ""), _int(_attr(elem, "filesize"))))
        elif name == "encoded" and family == "content":
            encoded = _text(elem)
        elif name == "description" and family in ("plain", "other"):
            description = _text(elem)
        elif name in ("content", "summary") and family == "atom":
            encoded = encoded or _text(elem)
        elif name == "summary" and family == "itunes":
            summary = _text(elem)
        elif name == "duration" and family == "itunes":
            ep["duration"] = parse_duration(_text(elem))
        elif name == "image" and family == "itunes":
            ep["image_url"] = _http_url(_attr(elem, "href")) or ep["image_url"]
        elif name == "thumbnail" and family == "media":
            ep["image_url"] = ep["image_url"] or _http_url(_attr(elem, "url"))
        elif name == "episode" and family in ("itunes", "podcast"):
            ep["episode_number"] = ep["episode_number"] if ep["episode_number"] is not None else _int(_text(elem))
        elif name == "season" and family in ("itunes", "podcast"):
            ep["season"] = ep["season"] if ep["season"] is not None else _int(_text(elem))
        elif name == "episodetype" and family == "itunes":
            value = _text(elem).lower()
            if value in ("full", "trailer", "bonus"):
                ep["episode_type"] = value
        elif name == "explicit" and family == "itunes":
            ep["explicit"] = _explicit(_text(elem))
        elif name == "chapters" and family == "podcast":
            url = _http_url(_attr(elem, "url"))
            if url:
                ep["chapters_url"] = url
                ep["chapters_type"] = _attr(elem, "type").lower()
        elif name == "transcript" and family == "podcast":
            url = _http_url(_attr(elem, "url"))
            if url:
                ep["transcripts"].append({
                    "url": url,
                    "type": _attr(elem, "type").lower(),
                    "language": _attr(elem, "language").lower(),
                    "rel": _attr(elem, "rel").lower(),
                })
        elif name == "person" and family == "podcast":
            person = {
                "name": _clip(_text(elem), 200),
                "role": _attr(elem, "role").lower() or "host",
                "group": _attr(elem, "group").lower() or "cast",
                "img": _http_url(_attr(elem, "img")),
                "href": _http_url(_attr(elem, "href")),
            }
            if person["name"]:
                ep["persons"].append(person)

    chosen = _pick_enclosure(enclosures)
    if chosen is None:
        return None
    ep["enclosure_url"], ep["enclosure_type"], ep["enclosure_length"] = chosen
    ep["guid"] = _clip(guid, MAX_TEXT_FIELD) or ep["enclosure_url"]

    rich, text = htmlclean.clean(encoded or description or summary)
    ep["notes_html"] = rich
    ep["notes_text"] = text
    if not ep["title"]:
        ep["title"] = htmlclean.summary(text, 80) or "Untitled episode"
    ep["content_hash"] = content_hash(ep)
    return ep


def _pick_enclosure(enclosures):
    if not enclosures:
        return None
    for url, mime, length in enclosures:
        if mime.startswith(AUDIO_TYPES):
            return url, mime, length
    for url, mime, length in enclosures:
        if mime.startswith(VIDEO_TYPES):
            return url, mime, length
    url, mime, length = enclosures[0]
    return url, mime or "audio/mpeg", length


def content_hash(ep):
    material = json.dumps([
        ep.get("title"), ep.get("enclosure_url"), ep.get("enclosure_type"), ep.get("enclosure_length"),
        ep.get("pub_date"), ep.get("duration"), ep.get("notes_html"), ep.get("image_url"),
        ep.get("episode_number"), ep.get("season"), ep.get("episode_type"), ep.get("chapters_url"),
        ep.get("transcripts"), ep.get("persons"), ep.get("link"),
    ], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(material.encode("utf-8")).hexdigest()
