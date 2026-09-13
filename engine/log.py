"""Logging setup: a rotating file under ~/.local/state, plus stderr when the
daemon runs in the foreground. Everything in the engine logs through
`logging.getLogger("podcast.<module>")` so `--debug` can turn on the wire-level
traces without touching call sites."""

import logging
import logging.handlers
import os
import sys

ROOT = "podcast"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 3


def get(name):
    return logging.getLogger(ROOT + "." + name)


def setup(log_path, debug=False, foreground=False):
    root = logging.getLogger(ROOT)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        foreground = True

    if foreground:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # asyncio's own logger is noisy about things we handle ourselves; keep its
    # warnings but route them into the same file.
    aio = logging.getLogger("asyncio")
    aio.setLevel(logging.WARNING)
    for handler in root.handlers:
        aio.addHandler(handler)
    return root


def tail(log_path, lines=100):
    """Last `lines` lines of the log, for the `log-tail` command."""
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                step = min(block, size)
                size -= step
                handle.seek(size)
                data = handle.read(step) + data
        return data.decode("utf-8", "replace").splitlines()[-lines:]
    except OSError:
        return []
