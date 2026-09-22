"""Concrete per-user XDG paths."""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


def secure_directory(path: Path, *, label: str = "directory") -> Path:
    """Create and verify a private directory without accepting symlink components."""
    missing: list[Path] = []
    current = path
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        if current == current.parent:
            break
        current = current.parent
    for component in (current, *reversed(missing)):
        if component.is_symlink():
            raise RuntimeError(f"{label} must not contain symlinks: {component}")
        if component in missing:
            with suppress(FileExistsError):
                component.mkdir(mode=0o700)
            if component.is_symlink():
                raise RuntimeError(f"{label} must not contain symlinks: {component}")
    try:
        status = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError(f"{label} is unavailable: {path}") from exc
    if not stat.S_ISDIR(status.st_mode):
        raise RuntimeError(f"{label} must be a directory: {path}")
    if status.st_uid != os.getuid():
        raise RuntimeError(f"{label} must be owned by UID {os.getuid()}: {path}")
    if stat.S_IMODE(status.st_mode) != 0o700:
        raise RuntimeError(f"{label} must have mode 0700: {path}")
    return path


@dataclass(frozen=True, slots=True)
class XDGPaths:
    config: Path
    data: Path
    cache: Path
    state: Path
    runtime: Path

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> XDGPaths:
        values = os.environ if env is None else env
        home = Path(values.get("HOME", str(Path.home())))
        uid = os.getuid()
        configured_runtime = values.get("XDG_RUNTIME_DIR") or None
        configured_homes: dict[str, Path] = {}
        defaults = {
            "XDG_CONFIG_HOME": home / ".config",
            "XDG_DATA_HOME": home / ".local/share",
            "XDG_CACHE_HOME": home / ".cache",
            "XDG_STATE_HOME": home / ".local/state",
        }
        for variable, default in defaults.items():
            raw = values.get(variable) or str(default)
            candidate = Path(raw)
            if not candidate.is_absolute():
                raise RuntimeError(f"{variable} must be an absolute path: {raw}")
            configured_homes[variable] = candidate
        if configured_runtime is not None and not Path(configured_runtime).is_absolute():
            raise RuntimeError(f"XDG_RUNTIME_DIR must be an absolute path: {configured_runtime}")
        runtime = Path(
            configured_runtime
            if configured_runtime is not None
            else str(Path(tempfile.gettempdir()) / f"labctl-{uid}")
        )
        if configured_runtime is not None:
            secure_directory(runtime, label="XDG runtime directory")
            runtime /= "labctl"
        secure_directory(runtime, label="labctl runtime directory")
        return cls(
            configured_homes["XDG_CONFIG_HOME"] / "labctl",
            configured_homes["XDG_DATA_HOME"] / "labctl",
            configured_homes["XDG_CACHE_HOME"] / "labctl",
            configured_homes["XDG_STATE_HOME"] / "labctl",
            runtime,
        )
