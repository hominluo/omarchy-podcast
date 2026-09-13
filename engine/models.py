"""Row -> wire-shape conversions shared by every subsystem.

The QML side works with two small shapes, `EpisodeSummary` and
`PodcastSummary`, plus a richer `EpisodeDetail` on request. Building them in
one place keeps the field names identical whether an episode arrives in a
queue broadcast, an inbox page or a search result.
"""

import json
import os

from . import htmlclean

# Joined query that carries everything an EpisodeSummary needs. Callers add
# WHERE / ORDER BY / LIMIT.
EPISODE_SELECT = """
SELECT e.*,
       p.title AS podcast_title, p.artwork_path AS podcast_artwork_path, p.image_url AS podcast_image_url,
       p.feed_url AS podcast_feed_url, p.funding_json AS podcast_funding_json, p.language AS podcast_language,
       d.status AS download_status, d.path AS download_path, d.bytes_total AS download_bytes_total,
       d.bytes_done AS download_bytes_done, d.keep AS download_keep,
       t.status AS transcript_status, t.source AS transcript_source,
       q.position AS queue_position,
       c.episode_id AS has_chapters_cache
FROM episodes e
JOIN podcasts p ON p.id = e.podcast_id
LEFT JOIN downloads d ON d.episode_id = e.id
LEFT JOIN transcripts t ON t.episode_id = e.id
LEFT JOIN queue q ON q.episode_id = e.id
LEFT JOIN chapters_cache c ON c.episode_id = e.id
"""


def _get(row, key, default=None):
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def artwork_for(row):
    """Local artwork wins; a remote URL is a workable fallback the shell can
    load directly (except WebP, which the cache job converts later)."""
    for key in ("artwork_path", "image_url", "podcast_artwork_path", "podcast_image_url"):
        value = _get(row, key, "")
        if value:
            return value
    return ""


def download_state(row):
    status = _get(row, "download_status", "")
    if status == "done":
        path = _get(row, "download_path", "")
        if path and os.path.exists(path):
            return "done"
        return "none"
    if status in ("queued", "downloading", "paused"):
        return "downloading" if status == "downloading" else "queued"
    return "none"


def transcript_state(row):
    status = _get(row, "transcript_status", "")
    if status == "complete":
        return _get(row, "transcript_source", "feed")
    if status in ("partial", "queued"):
        return "partial"
    return "none"


def episode_summary(row):
    duration = _get(row, "duration")
    return {
        "id": _get(row, "id"),
        "podcastId": _get(row, "podcast_id"),
        "podcastTitle": _get(row, "podcast_title", ""),
        "title": _get(row, "title", ""),
        "pubDate": _get(row, "pub_date"),
        "duration": duration,
        "position": float(_get(row, "position", 0) or 0),
        "played": bool(_get(row, "played", 0)),
        "state": _get(row, "state", "inbox"),
        "download": download_state(row),
        "fileSize": _get(row, "download_bytes_total") or _get(row, "enclosure_length"),
        "artwork": artwork_for(row),
        "episodeNumber": _get(row, "episode_number"),
        "season": _get(row, "season"),
        "episodeType": _get(row, "episode_type", "full"),
        "hasChapters": bool(_get(row, "chapters_url", "")) or _get(row, "has_chapters_cache") is not None,
        "transcript": transcript_state(row),
        "hasTranscriptSource": _get(row, "transcripts_json", "[]") not in ("", "[]"),
        "queued": _get(row, "queue_position") is not None,
        "notesText": htmlclean.summary(_get(row, "notes_text", ""), 220),
    }


def episode_detail(row):
    detail = episode_summary(row)
    detail.update({
        "notesHtml": _get(row, "notes_html", ""),
        "notesFullText": _get(row, "notes_text", ""),
        "link": _get(row, "link", ""),
        "enclosureUrl": _get(row, "enclosure_url", ""),
        "enclosureType": _get(row, "enclosure_type", ""),
        "explicit": bool(_get(row, "explicit", 0)),
        "chaptersUrl": _get(row, "chapters_url", ""),
        "transcriptSources": _json(_get(row, "transcripts_json", "[]"), []),
        "persons": _json(_get(row, "persons_json", "[]"), []),
        "funding": _json(_get(row, "podcast_funding_json", "[]"), []),
        "localPath": _get(row, "download_path", "") if download_state(row) == "done" else "",
        "language": _get(row, "podcast_language", ""),
        "feedUrl": _get(row, "podcast_feed_url", ""),
        "guid": _get(row, "guid", ""),
        "lastPlayedAt": _get(row, "last_played_at"),
        "playedAt": _get(row, "played_at"),
    })
    return detail


PODCAST_SELECT = """
SELECT p.*,
       (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id = p.id) AS total_count,
       (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id = p.id AND e.played = 0) AS unplayed_count,
       (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id = p.id AND e.played = 0 AND e.state = 'inbox') AS new_count,
       (SELECT COUNT(*) FROM downloads d JOIN episodes e ON e.id = d.episode_id WHERE e.podcast_id = p.id AND d.status = 'done') AS downloaded_count,
       (SELECT MAX(e.pub_date) FROM episodes e WHERE e.podcast_id = p.id) AS newest_pub_date
FROM podcasts p
"""


def podcast_summary(row):
    return {
        "id": _get(row, "id"),
        "title": _get(row, "title", ""),
        "author": _get(row, "author", ""),
        "artwork": _get(row, "artwork_path", "") or _get(row, "image_url", ""),
        "feedUrl": _get(row, "feed_url", ""),
        "link": _get(row, "link", ""),
        "language": _get(row, "language", ""),
        "description": htmlclean.summary(_get(row, "description_text", ""), 300),
        "counts": {
            "total": _get(row, "total_count", 0),
            "unplayed": _get(row, "unplayed_count", 0),
            "new": _get(row, "new_count", 0),
            "downloaded": _get(row, "downloaded_count", 0),
        },
        "speed": _get(row, "speed"),
        "autoDownload": _get(row, "auto_download"),
        "skipIntroSec": _get(row, "skip_intro_sec"),
        "skipOutroSec": _get(row, "skip_outro_sec"),
        "autoQueue": _get(row, "auto_queue", "none"),
        "lastFetchAt": _get(row, "last_fetch_at"),
        "lastFetchOk": bool(_get(row, "last_fetch_ok", 1)),
        "lastError": _get(row, "last_error", ""),
        "subscribedAt": _get(row, "subscribed_at"),
        "newestPubDate": _get(row, "newest_pub_date"),
        "podcastGuid": _get(row, "podcast_guid"),
        "itunesId": _get(row, "itunes_id"),
    }


def podcast_detail(row):
    detail = podcast_summary(row)
    detail["descriptionHtml"] = _get(row, "description_html", "")
    detail["descriptionText"] = _get(row, "description_text", "")
    detail["funding"] = _json(_get(row, "funding_json", "[]"), [])
    return detail


def _json(raw, default):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if value is not None else default
