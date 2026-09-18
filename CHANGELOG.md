# Changelog

## 1.0.3 — 2026-09-18

Security hardening after the second marketplace review.

- Whisper models are pinned to one revision of `ggerganov/whisper.cpp`
  (`5359861c739e955e79d9a303bcbc70fb988958b1`) and verified against a recorded
  size and SHA-256 before use. Downloads are size-capped, resumed only when the
  server's byte range agrees with the partial file (which now has an
  unpredictable name recorded in a sidecar), stall- and time-limited, checked
  for free space first, and hashed before being renamed into place. A file
  that does not match is deleted and fetched again; two failures in a row stop
  the retries until the setting changes.
- ffmpeg and whisper-cli run in their own process groups under a wall-clock
  limit, are killed as a group on cancel, and are stopped before the daemon
  re-executes itself. mpv gets a `kill()` after `terminate()` times out.
- The shell never loads a remote image: search results and feed previews are
  fetched, sniffed and re-encoded by the daemon into a bounded thumbnail cache
  (`artwork-thumb`), the raw-bytes fallback when ffmpeg refuses an image is
  gone, and the server's content type must be an image.
- Downloads are bounded by the absolute cap whatever `Content-Length` says;
  free space is re-checked against the server's size; only audio extensions
  reach the library; `cover.jpg` and cross-filesystem moves go through a fresh
  file of ours and never follow a planted link.
- The daemon refuses to start without a private per-user runtime directory
  (no shared temp fallback), checks the peer uid on every socket connection,
  caps clients, in-flight requests and unread output, confines `opml-export`
  to the home directory (0600, never through a symlink) and `opml-import` to
  regular files, opens the lock file `O_NOFOLLOW`, creates the log 0600 and
  redacts credentials and query strings from logged URLs.
- Feeds and OPML files that declare a DTD are refused; every string and list a
  feed contributes has a ceiling; search results are validated and sliced.
- Sync requires `https://` (plain `http://` only for `localhost`), quotes the
  username and device id in URL paths, validates rewritten feed URLs, and adds
  at most two hundred remote subscriptions per cycle.
- An `https` link is never followed down to plain `http`.

## 1.0.2 — 2026-09-17

Every file write goes through one symlink-safe primitive (`engine/fsio.py`).

## 1.0.1 — 2026-09-14

Feeds that refuse agents naming a URL; long enclosure redirect chains.

## 1.0.0 — 2026-09-14

First release.
