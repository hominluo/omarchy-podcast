# Changelog

## 1.1.1 — 2026-11-03

- Up Next rows show their artwork again. A cached path whose file had gone was
  handed to the shell anyway, so those rows drew the placeholder glyph forever
  while the podcast's own cover sat on disk. Missing files now read as no
  artwork, and the artwork sweep forgets them so they are fetched again.
- The bar glyph keeps the bar's text colour in every state. It used to fade to
  60% while paused, which read as a dimmed or broken icon beside its
  neighbours.
- The transport row is one line of controls: equal heights, even spacing, a
  wider play button, and larger skip glyphs, instead of five boxes of five
  different sizes.
- The artwork placeholder is centred by its painted bounds, so it no longer
  sits against the bottom edge of a tile.

## 1.1.0 — 2026-11-01

- Up Next also reorders with `Shift` + the arrow keys, the gesture most list
  UIs train your hands to expect. `J` / `K` are unchanged, and a bare arrow
  key still only moves the cursor. (Contributed by @aptaluke, PR #2.)
- The keyboard help lists the shifted arrows.

## 1.0.4 — 2026-11-01

Correctness, privacy and security fixes from a full review of the daemon and
the QML client.

- The DOCTYPE guard walks the XML prolog token by token instead of matching a
  64 KiB slice, so a long comment or processing instruction can no longer hide
  a DTD (and its entities) from it. A UTF-8 BOM is stepped over as well.
- Feed URLs in log messages are redacted (userinfo and query string), so a
  private feed token is no longer written to the log in clear.
- A public feed can no longer redirect a fetch to a private address: literal
  private hosts are refused (loopback stays allowed for local feeds), and a
  public→private redirect hop is rejected. This covers feeds, enclosures,
  artwork, chapters and transcript links.
- `play()` only removes the episode from Up Next and records "last episode"
  once mpv has accepted `loadfile`; a refused load restores the previous state
  instead of losing the queue entry.
- A failed chapters fetch is retried after 15 minutes instead of being cached
  as an empty list forever. Empty rows written by older releases are retried
  too.
- `keepDownloads` caps played downloads only. The old query counted every
  download and stopped at the first unplayed row, which could remove played
  episodes even when fewer than the cap had been played.
- `HTTPError` responses (304, 4xx, 5xx and the `probe_headers` 3xx path) are
  closed deterministically instead of relying on the garbage collector.
- The QML lint debt is gone (no block-scoped `var` hoisting, no id shadowing),
  and the keyboard help matches the implemented keys.

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
- After review: a fully downloaded model whose verification was interrupted
  is hashed on the next run instead of fetched again; `opml-export` also
  accepts the configured download folder as a root; `opml-import` never
  blocks on a FIFO; stray converted WAVs are swept at start-up; time-limit
  errors name ffmpeg or whisper-cli rather than `nice`; the thumbnail queue
  in the shell issues one request per URL (a batch could record another
  podcast's picture under the wrong URL).

## 1.0.2 — 2026-09-17

Every file write goes through one symlink-safe primitive (`engine/fsio.py`).

## 1.0.1 — 2026-09-14

Feeds that refuse agents naming a URL; long enclosure redirect chains.

## 1.0.0 — 2026-09-14

First release.
