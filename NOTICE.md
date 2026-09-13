# Notices

## Silence skipping

`mpv/skipsilence.lua` is this plugin's own MIT-licensed implementation of a
well-known technique: keep ffmpeg's `silencedetect` filter in mpv's audio
chain, and raise the playback speed while it reports silence. The idea comes
from NewPipe's "fast-forward during silence" feature and from
[ferreum/mpv-skipsilence](https://codeberg.org/ferreum/mpv-skipsilence),
which is GPL-2.0 licensed. No code from either project is included here; the
script was written from scratch against mpv's documented Lua API so that the
plugin can stay MIT.

## Runtime dependencies

The plugin executes but does not bundle:

| Tool | License | Used for |
|---|---|---|
| [mpv](https://mpv.io) | GPL-2.0-or-later / LGPL-2.1 | all audio playback, seeking, speed and chapters |
| [mpv-mpris](https://github.com/hoyon/mpv-mpris) | MIT | publishing the player on MPRIS (media keys, Omarchy's media widget) |
| [ffmpeg](https://ffmpeg.org) | LGPL-2.1 / GPL-2.0 | normalising cover art and preparing audio for transcription |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) (`whisper-cli`) | MIT | local transcription |
| [ggml](https://github.com/ggml-org/ggml) (`ggml-vulkan`) | MIT | the GPU backend whisper.cpp loads when present |
| `omarchy-notification-send`, `omarchy-launch-browser` | MIT (Omarchy) | new-episode notifications; opening links from show notes |

These are invoked as separate processes, not linked or redistributed, so their
licenses do not extend to this plugin.

## Speech models

Transcription downloads OpenAI Whisper models converted for whisper.cpp from
[huggingface.co/ggerganov/whisper.cpp](https://huggingface.co/ggerganov/whisper.cpp)
on first use. The models are MIT licensed by OpenAI and are stored under
`~/.cache/omarchy/podcast/models/`; they are never redistributed by this plugin.

## Catalogues

Search uses the [iTunes Search API](https://performance-partners.apple.com/search-api)
by default and, when the user supplies a key, the
[Podcast Index API](https://podcastindex.org). Results, artwork and feeds
remain the property of their publishers; the plugin caches artwork and feed
contents on the user's machine only.

Apple Podcasts is a trademark of Apple Inc. This plugin is not affiliated with,
endorsed by, or sponsored by Apple, Podcast Index, gpodder.net or Nextcloud.
