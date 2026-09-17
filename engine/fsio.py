"""Filesystem primitives that never trust a pathname twice.

Every file the daemon writes goes through here. A new file is created with an
unpredictable name and O_CREAT|O_EXCL|O_NOFOLLOW, so a symlink planted at a
guessable name cannot redirect the write; the descriptor is then checked to be
our own fresh regular file. Whole-file writes are fsynced and renamed into
place. Resumable partial files are opened O_NOFOLLOW and checked the same way
before a byte is appended.
"""

import errno
import os
import secrets
import stat

_NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _verify(fd, path, fresh):
    info = os.fstat(fd)
    ours = stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
    if not ours or (fresh and info.st_nlink != 1):
        os.close(fd)
        raise OSError(errno.EPERM, "not a regular file of ours; refusing to write", path)


def open_new(directory, prefix, suffix="", mode=0o600):
    """Create an unpredictable new file in `directory`. Returns (fd, path)."""
    os.makedirs(directory, exist_ok=True)
    for _ in range(32):
        path = os.path.join(directory, prefix + secrets.token_hex(8) + suffix)
        try:
            fd = os.open(path, _NEW, mode)
        except FileExistsError:
            continue
        _verify(fd, path, fresh=True)
        return fd, path
    raise OSError(errno.EEXIST, "could not create a temporary file", directory)


def open_nofollow(path, flags, mode=0o600):
    """Open `path` without following a symlink standing in its place, and
    refuse anything that is not our own regular file. With O_CREAT|O_EXCL
    the file must also be brand new."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    _verify(fd, path, fresh=bool(flags & os.O_EXCL))
    return fd


def atomic_write(path, data, mode=0o600):
    """Replace `path` with `data` (str or bytes) through a temp beside it."""
    directory = os.path.dirname(path) or "."
    fd, tmp = open_new(directory, "." + os.path.basename(path) + ".", ".tmp", mode)
    try:
        handle = os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        _unlink_quiet(tmp)
        raise
    try:
        with handle:
            handle.write(data.encode("utf-8") if isinstance(data, str) else data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        _unlink_quiet(tmp)
        raise
    _fsync_dir(directory)


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _fsync_dir(directory):
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
