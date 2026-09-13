"""Wires the subsystems into an Engine.

Kept apart from engine.py so the composition root does not import every
module, and so tests can build an Engine with only the pieces they need. Each
subsystem module registers its own commands with `protocol.command` on
import, and exposes an object with `start()` and `stop(restart, quit_mpv)`.

Start order matters: playback installs its callbacks on the mpv client
before the client connects, and both need the library and queue.
"""

from .library import Library
from .playback import Playback
from .player import MpvClient
from .queue import Queue


def attach(engine):
    engine.library = Library(engine)
    engine.queue = Queue(engine)
    engine.mpv = MpvClient(engine)
    engine.playback = Playback(engine)
    engine.subsystems = [engine.library, engine.queue, engine.playback, engine.mpv]
    return engine
