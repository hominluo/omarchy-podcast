"""Wires the subsystems into an Engine.

Kept apart from engine.py so the composition root does not import every
module, and so tests can build an Engine with only the pieces they need. Each
subsystem module registers its own commands with `protocol.command` on
import, and exposes an object with `start()` and `stop(restart, quit_mpv)`.
"""


def attach(engine):
    # Subsystems land here milestone by milestone: player, playback, library,
    # search, downloads, transcripts, sync, scheduler.
    return engine
