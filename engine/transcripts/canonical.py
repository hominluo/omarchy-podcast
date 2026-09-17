"""The one transcript shape everything else reads and writes.

    {"version": 1, "source": "feed" | "whisper", "sourceType": "text/vtt",
     "language": "en", "timed": true, "status": "complete" | "partial",
     "model": "", "segments": [{"startTime": 12.3, "endTime": 15.0,
                                "speaker": "", "body": "..."}]}

Files are keyed by the episode, not its database id, so a rebuilt library
finds its transcripts again.
"""

import hashlib
import json
import math
import os

from .. import fsio

VERSION = 1


def episode_key(feed_url, guid):
    return hashlib.sha1((str(feed_url) + "\n" + str(guid)).encode("utf-8")).hexdigest()[:24]


def path_for(paths, key):
    return os.path.join(paths.transcripts_dir, key + ".json")


def new_document(source, source_type="", language="", timed=True, status="complete", model="", segments=None):
    return {
        "version": VERSION,
        "source": source,
        "sourceType": source_type,
        "language": language,
        "timed": bool(timed),
        "status": status,
        "model": model,
        "segments": list(segments or []),
    }


def segment(start, end, body, speaker=""):
    start = _finite(start, 0.0)
    end = _finite(end, None) if end is not None else None
    return {
        "startTime": round(start, 3),
        "endTime": round(end, 3) if end is not None else None,
        "speaker": str(speaker or "").strip(),
        "body": " ".join(str(body or "").split()),
    }


def _finite(value, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if math.isnan(number) or math.isinf(number):
        return fallback
    return max(0.0, number)


def load(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("segments"), list):
        return None
    return doc


def save(path, doc):
    fsio.atomic_write(path, json.dumps(doc, ensure_ascii=False, separators=(",", ":")))


def paragraphs(segments, max_chars=320, max_seconds=14.0):
    """Merge consecutive cues by the same speaker into readable paragraphs.
    Cue-level timing is kept on the first cue so tap-to-seek still lands."""
    out = []
    for seg in segments:
        if out:
            last = out[-1]
            same_speaker = (last.get("speaker") or "") == (seg.get("speaker") or "")
            length_ok = len(last["body"]) + len(seg["body"]) + 1 <= max_chars
            span_ok = (seg.get("endTime") or seg["startTime"]) - last["startTime"] <= max_seconds
            ends_sentence = last["body"].rstrip().endswith((".", "!", "?", "。", "！", "？"))
            if same_speaker and length_ok and span_ok and not ends_sentence:
                last["body"] = (last["body"] + " " + seg["body"]).strip()
                last["endTime"] = seg.get("endTime") if seg.get("endTime") is not None else last.get("endTime")
                continue
        out.append(dict(seg))
    return out
