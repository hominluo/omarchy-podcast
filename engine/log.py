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


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """The log is created 0600 and never opened through a symlink: it
    records feed and enclosure URLs, which private feeds key their tokens
    into. Rotation renames, so the backups keep the mode."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        return open(fd, self.mode, encoding=self.encoding, errors=self.errors)


def setup(log_path, debug=False, foreground=False):
    root = logging.getLogger(ROOT)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        try:
            os.chmod(log_path, 0o600)          # a file an older release left world-readable
        except OSError:
            pass
        file_handler = _PrivateRotatingFileHandler(
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
