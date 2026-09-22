from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from labctl.locks import LockConflict, file_lock
from labctl.state import StateError, load_state, save_state
from labctl.trust import TrustStore


def test_atomic_state_permissions_and_validation(tmp_path: Path) -> None:
    path = tmp_path / "instances" / "LX001.json"
    save_state(path, {"schema_version": 1, "id": "LX001", "provider_uri": "qemu:///system"})
    assert load_state(path)["id"] == "LX001"
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    path.write_text("{", encoding="utf-8")
    with pytest.raises(StateError, match="unreadable"):
        load_state(path)


def test_migration_backup_and_future_rejection(tmp_path: Path) -> None:
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"schema_version": 0, "lab_id": "LX001", "uri": "qemu:///session"}))
    migrated = load_state(old)
    assert migrated == {"schema_version": 1, "id": "LX001", "provider_uri": "qemu:///session"}
    assert old.with_suffix(".json.v0.bak").exists()
    future = tmp_path / "future.json"
    future.write_text('{"schema_version": 99, "id": "LX001", "provider_uri": "x"}')
    with pytest.raises(StateError, match="future"):
        load_state(future)


def test_state_v1_accepts_only_absolute_optional_storage_path(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    document = {
        "schema_version": 1,
        "id": "LX001",
        "provider_uri": "qemu:///system",
        "storage_path": str(tmp_path / "storage"),
    }
    save_state(path, document)
    assert load_state(path)["storage_path"] == document["storage_path"]

    document["storage_path"] = "relative"
    with pytest.raises(StateError, match="storage_path must be an absolute path"):
        save_state(path, document)


def test_lock_conflict(tmp_path: Path) -> None:
    path = tmp_path / "lab.lock"
    with file_lock(path), pytest.raises(LockConflict), file_lock(path):
        pass


def test_trust_store_permissions(tmp_path: Path) -> None:
    store = TrustStore(tmp_path / "trust.json")
    store.accept("abc")
    assert store.accepted("abc")
    assert os.stat(tmp_path / "trust.json").st_mode & 0o777 == 0o600
