#!/usr/bin/env python3
"""Launcher for the Omarchy-Podcast engine.

The shell runs `python3 podcastd.py serve` when it cannot reach the daemon's
socket. Everything real lives in the `engine` package next to this file; the
launcher only makes two arrangements that have to happen before that import:

  * Bytecode goes to ~/.cache/omarchy/podcast/pycache, never to a __pycache__
    inside the plugin folder. The shell watches that folder with inotify and
    reloads the plugin on every write, so twenty .pyc files landing on first
    import would reload the bar twenty times.
  * The plugin folder is put on sys.path so `engine` imports no matter what
    the caller's working directory is (Quickshell spawns us with none).
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

_cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
sys.pycache_prefix = os.path.join(_cache_home, "omarchy", "podcast", "pycache")

if HERE not in sys.path:
    sys.path.insert(0, HERE)

from engine.cli import main  # noqa: E402  (sys.path must be set first)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], launcher=os.path.abspath(__file__)))
