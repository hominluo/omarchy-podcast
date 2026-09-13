"""Feed-supplied transcript formats -> canonical segments.

Every parser takes bytes and returns (segments, timed). They are forgiving:
a VTT with `MM:SS.mmm` stamps, an SRT with commas or dots, JSON that spells
the Podcasting 2.0 fields slightly differently, HTML with <time> tags, and
plain text with or without `[hh:mm:ss]` prefixes all come out the other end.
"""

import html
import json
import re
from html.parser import HTMLParser

from .canonical import segment

MAX_BYTES = 5 * 1024 * 1024
PREFERENCE = ("application/json", "text/vtt", "application/x-subrip", "application/srt", "text/srt", "text/html", "text/plain")

_TIME = re.compile(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_TIME_PLAIN = re.compile(r"^\s*[\[(]?(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?[\])]?\s*[-–:]?\s*")
_VOICE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]*)>", re.I)
_TAGS = re.compile(r"<[^>]+>")


def decode(data):
    if isinstance(data, str):
        return data
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", "replace")


def _stamp(match):
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    millis = match.group(4) or "0"
    millis = int(millis.ljust(3, "0")[:3])
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def _strip_cue_markup(text):
    text = _TAGS.sub("", text)
    return html.unescape(text)


def parse_vtt(data):
    text = decode(data).replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text.strip())
    segments = []
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines or lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        stamps = _TIME.findall(lines[timing_index])
        if len(stamps) < 2:
            continue
        start = _stamp(_TIME.search(lines[timing_index]))
        end = _stamp(list(_TIME.finditer(lines[timing_index]))[1])
        body = " ".join(lines[timing_index + 1:])
        speaker = ""
        voice = _VOICE.search(body)
        if voice:
            speaker = voice.group(1).strip()
        body = _strip_cue_markup(body).strip()
        if body:
            segments.append(segment(start, end, body, speaker))
    _split_named_speakers(segments)
    return _sorted(segments), True


_NAMED = re.compile(r"^([A-Z][A-Za-z0-9 .'_-]{0,40}):\s+(.*)$")


def _split_named_speakers(segments):
    """Cues that carry a `NAME:` prefix in most lines get it moved into the
    speaker field; a stray one is left alone (it may be part of the words)."""
    candidates = [seg for seg in segments if not seg["speaker"] and _NAMED.match(seg["body"])]
    if not segments or len(candidates) < len(segments) * 0.3:
        return
    for seg in candidates:
        match = _NAMED.match(seg["body"])
        seg["speaker"], seg["body"] = match.group(1), match.group(2)


def parse_srt(data):
    text = decode(data).replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text.strip())
    segments = []
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        stamps = list(_TIME.finditer(lines[timing_index]))
        if len(stamps) < 2:
            continue
        body = _strip_cue_markup(" ".join(lines[timing_index + 1:])).strip()
        if body:
            segments.append(segment(_stamp(stamps[0]), _stamp(stamps[1]), body))
    _split_named_speakers(segments)
    return _sorted(segments), True


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_json(data):
    try:
        payload = json.loads(decode(data))
    except ValueError:
        return [], False
    items = payload.get("segments") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return [], False
    segments = []
    for item in items:
        if not isinstance(item, dict):
            continue
        start = _number(item.get("startTime", item.get("start")))
        end = _number(item.get("endTime", item.get("end")))
        body = item.get("body", item.get("text"))
        if start is None or not body:
            continue
        # Some generators write milliseconds.
        if start > 100000 and (end is None or end > 100000):
            start /= 1000.0
            end = end / 1000.0 if end is not None else None
        segments.append(segment(start, end, body, item.get("speaker", "")))
    return _sorted(segments), bool(segments)


class _HtmlTranscript(HTMLParser):
    """Blocks of text, with a time taken from <time datetime> / <time> text or
    data-time / data-start attributes, and a speaker from <cite>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self._buf = []
        self._time = None
        self._speaker = ""
        self._in_cite = False
        self._in_time = False
        self._skip = 0

    def _flush(self):
        body = " ".join("".join(self._buf).split())
        if body:
            self.blocks.append((self._time, self._speaker, body))
        self._buf = []
        self._time = None
        self._speaker = ""

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style", "h1", "h2", "h3", "h4", "h5", "h6", "nav", "header", "footer"):
            self._flush()
            self._skip += 1
            return
        if tag in ("p", "div", "section", "li", "tr", "article"):
            self._flush()
        for key in ("data-time", "data-start", "data-timestamp", "data-seconds"):
            if key in attrs:
                self._time = _seconds_from(attrs[key])
        if tag == "time":
            self._in_time = True
            if attrs.get("datetime"):
                self._time = _seconds_from(attrs["datetime"])
        elif tag == "cite":
            self._in_cite = True
        elif tag == "br":
            self._buf.append(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "h1", "h2", "h3", "h4", "h5", "h6", "nav", "header", "footer"):
            self._skip = max(0, self._skip - 1)
            return
        if tag == "time":
            self._in_time = False
        elif tag == "cite":
            self._in_cite = False
        elif tag in ("p", "div", "section", "li", "tr", "article"):
            self._flush()

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_time:
            if self._time is None:
                self._time = _seconds_from(data)
            return
        if self._in_cite:
            self._speaker = (self._speaker + " " + data).strip().rstrip(":")
            return
        self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _seconds_from(value):
    text = str(value or "").strip()
    match = _TIME_PLAIN.match(text)
    if match and (match.group(1) or match.group(2)):
        hours = int(match.group(1) or 0)
        return hours * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + int((match.group(4) or "0").ljust(3, "0")[:3]) / 1000.0
    number = _number(text)
    if number is None:
        return None
    return number / 1000.0 if number > 100000 else number


def parse_html(data):
    parser = _HtmlTranscript()
    try:
        parser.feed(decode(data))
        parser.close()
    except Exception:  # noqa: BLE001
        return parse_text(data)
    timed = any(t is not None for t, _, _ in parser.blocks)
    segments = []
    last = 0.0
    for stamp, speaker, body in parser.blocks:
        if stamp is None:
            stamp = last
        last = stamp
        segments.append(segment(stamp, None, body, speaker))
    return (_sorted(segments) if timed else segments), timed


def parse_text(data):
    text = decode(data).replace("\r\n", "\n").replace("\r", "\n")
    segments = []
    timed = False
    last = 0.0
    for raw in re.split(r"\n\s*\n|\n(?=\s*[\[(]?\d{1,2}:\d{2})", text):
        line = " ".join(raw.split())
        if not line:
            continue
        match = _TIME_PLAIN.match(line)
        stamp = None
        if match and (match.group(1) or match.group(2)):
            stamp = _seconds_from(match.group(0))
            line = line[match.end():].strip()
            timed = True
        if stamp is None:
            stamp = last
        last = stamp
        speaker = ""
        named = re.match(r"^([A-Z][A-Za-z0-9 .'_-]{0,40}):\s+(.*)$", line)
        if named:
            speaker, line = named.group(1), named.group(2)
        if line:
            segments.append(segment(stamp, None, line, speaker))
    return segments, timed


PARSERS = {
    "text/vtt": parse_vtt,
    "application/x-subrip": parse_srt,
    "application/srt": parse_srt,
    "text/srt": parse_srt,
    "application/json": parse_json,
    "text/html": parse_html,
    "text/plain": parse_text,
}


def parse(data, mime):
    """Dispatch on the declared type, sniffing when it lies."""
    if len(data) > MAX_BYTES:
        data = data[:MAX_BYTES]
    mime = str(mime or "").split(";")[0].strip().lower()
    head = decode(data[:2000]).lstrip()
    if head.startswith("WEBVTT"):
        mime = "text/vtt"
    elif head.startswith(("{", "[")) and mime not in ("text/html",):
        mime = "application/json"
    elif re.match(r"^\d+\s*\n\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->", head):
        mime = "application/x-subrip"
    elif head.lower().startswith(("<!doctype", "<html", "<div", "<p")):
        mime = "text/html"
    parser = PARSERS.get(mime, parse_text)
    segments, timed = parser(data)
    if not segments and parser is not parse_text:
        segments, timed = parse_text(data)
    _fill_end_times(segments)
    return segments, timed


def _sorted(segments):
    return sorted(segments, key=lambda s: s["startTime"])


def _fill_end_times(segments):
    for index, seg in enumerate(segments):
        if seg.get("endTime") is None:
            seg["endTime"] = segments[index + 1]["startTime"] if index + 1 < len(segments) else None


def choose_source(sources, language=""):
    """Pick the transcript entry to fetch from a `podcast:transcript` list:
    richest format first, the feed's language before others."""
    wanted = (language or "").split("-")[0].lower()

    def rank(entry):
        mime = str(entry.get("type") or "").lower()
        try:
            fmt = PREFERENCE.index(mime)
        except ValueError:
            fmt = len(PREFERENCE)
        lang = str(entry.get("language") or "").split("-")[0].lower()
        lang_rank = 0 if (not wanted or not lang or lang == wanted) else 1
        captions = 1 if str(entry.get("rel") or "").lower() == "captions" else 0
        return (lang_rank, fmt, captions)

    usable = [entry for entry in sources or [] if str(entry.get("url") or "").startswith(("http://", "https://"))]
    if not usable:
        return None
    return sorted(usable, key=rank)[0]
