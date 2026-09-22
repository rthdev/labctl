from __future__ import annotations

import os
from pathlib import Path

import pytest

from labctl.locks import LockConflict, file_lock
from labctl.paths import XDGPaths


def test_file_lock_rejects_symlink_and_non_regular_targets(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    locks.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    symlink = locks / "symlink.lock"
    symlink.symlink_to(victim)
    directory = locks / "directory.lock"
    directory.mkdir()

    with pytest.raises(LockConflict, match="secure lock"), file_lock(symlink):
        pass
    with pytest.raises(LockConflict, match="regular file"), file_lock(directory):
        pass

    assert victim.read_text(encoding="utf-8") == "keep"


def test_file_lock_rejects_insecure_or_foreign_lock_directory(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    locks.mkdir(mode=0o755)

    with pytest.raises(LockConflict, match="mode 0700"), file_lock(locks / "lab.lock"):
        pass


def test_xdg_runtime_fallback_rejects_precreated_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / f"labctl-{os.getuid()}"
    victim = tmp_path / "victim"
    victim.mkdir()
    runtime.symlink_to(victim, target_is_directory=True)
    monkeypatch.setattr("labctl.paths.tempfile.gettempdir", lambda: str(tmp_path))

    with pytest.raises(RuntimeError, match="runtime directory"):
        XDGPaths.from_environment({"HOME": str(tmp_path / "home")})
