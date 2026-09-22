"""Nonblocking advisory locks.

Deadlock-safe acquisition order is provider, image digest, lab ID, then VM name.
Callers needing multiple locks must acquire lexical keys within each level.
"""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from labctl.paths import secure_directory


class LockConflict(RuntimeError):
    pass


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    try:
        secure_directory(path.parent, label="lock directory")
        if path.exists() and not path.is_symlink() and not path.is_file():
            raise LockConflict(f"lock target must be a regular file: {path}")
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise LockConflict(f"lock target must be a regular file: {path}")
        if status.st_uid != os.getuid():
            raise LockConflict(f"secure lock must be owned by UID {os.getuid()}: {path}")
        if stat.S_IMODE(status.st_mode) != 0o600:
            raise LockConflict(f"secure lock must have mode 0600: {path}")
    except LockConflict:
        if "descriptor" in locals():
            os.close(descriptor)
        raise
    except (OSError, RuntimeError) as exc:
        raise LockConflict(f"could not open secure lock {path}: {exc}") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockConflict(f"resource is locked by another labctl operation: {path}") from exc
        yield
    finally:
        os.close(descriptor)
