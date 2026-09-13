"""Wires the subsystems into an Engine.

Kept apart from engine.py so the composition root does not import every
module, and so tests can build an Engine with only the pieces they need. Each
subsystem module registers its own commands with `protocol.command` on
import, and exposes an object with `start()` and `stop(restart, quit_mpv)`.

Start order matters: playback installs its callbacks on the mpv client
before the client connects, and both need the library and queue.
"""

from . import opml  # noqa: F401  (registers its commands)
from .artwork import ArtworkCache
from .chapters import Chapters
from .downloads import Downloads
from .library import Library
from .playback import Playback
from .player import MpvClient
from .queue import Queue
from .scheduler import Scheduler
from .search import Search
from .transcripts.manager import Transcripts


def attach(engine):
    engine.library = Library(engine)
    engine.queue = Queue(engine)
    engine.artwork = ArtworkCache(engine)
    engine.chapters = Chapters(engine)
    engine.downloads = Downloads(engine)
    engine.transcripts = Transcripts(engine)
    engine.search = Search(engine)
    engine.mpv = MpvClient(engine)
    engine.playback = Playback(engine)
    engine.scheduler = Scheduler(engine)
    engine.housekeeping_hooks = []
    engine.subsystems = [
        engine.library, engine.queue, engine.artwork, engine.chapters, engine.search,
        engine.downloads, engine.transcripts, engine.playback, engine.mpv, engine.scheduler,
    ]
    return engine
