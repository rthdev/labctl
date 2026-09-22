"""Validated, atomic, versioned JSON state persistence."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any


class StateError(RuntimeError):
    pass


def _validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise StateError("state root must be an object")
    if data.get("schema_version") != 1:
        raise StateError("state schema_version must be 1")
    if not isinstance(data.get("id"), str) or not isinstance(data.get("provider_uri"), str):
        raise StateError("state requires string id and provider_uri")
    allowed = {
        "schema_version",
        "id",
        "provider_uri",
        "provider",
        "state",
        "instance_uid",
        "storage_path",
        "network",
        "vm_order",
        "image_digests",
        "definition_digest",
        "identity_file",
        "vms",
        "cleanup",
        "cleanup_failures",
    }
    unknown = data.keys() - allowed
    if unknown:
        raise StateError(f"unknown state field: {sorted(unknown)[0]}")
    storage_path = data.get("storage_path")
    if storage_path is not None and (
        not isinstance(storage_path, str) or not Path(storage_path).is_absolute()
    ):
        raise StateError("storage_path must be an absolute path")
    lifecycle = data.get("state")
    if lifecycle is not None and lifecycle not in {
        "provisioning",
        "ready",
        "running",
        "stopped",
        "degraded",
        "failed_cleanup",
    }:
        raise StateError(f"invalid lifecycle state: {lifecycle!r}")
    order = data.get("vm_order")
    if order is not None and (
        not isinstance(order, list) or not all(isinstance(item, str) for item in order)
    ):
        raise StateError("vm_order must be a string list")
    vms = data.get("vms")
    if vms is not None and (
        not isinstance(vms, dict)
        or not all(isinstance(name, str) and isinstance(vm, dict) for name, vm in vms.items())
    ):
        raise StateError("vms must be an object of VM objects")
    return data


def validate_state(data: Any) -> dict[str, Any]:
    """Validate an in-memory state document without performing I/O."""
    return _validate(data)


def save_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically persist a JSON object and its directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def save_state(path: Path, data: dict[str, Any]) -> None:
    _validate(data)
    save_json(path, data)


def load_state(path: Path) -> dict[str, Any]:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"state is unreadable: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError("state root must be an object")
    version = data.get("schema_version")
    if isinstance(version, int) and version > 1:
        raise StateError(f"future state schema version {version} is not supported")
    if version == 0:
        backup = path.with_suffix(path.suffix + ".v0.bak")
        if backup.exists():
            raise StateError(f"migration backup already exists: {backup}")
        backup.write_bytes(path.read_bytes())
        os.chmod(backup, 0o600)
        migrated = {"schema_version": 1, "id": data.get("lab_id"), "provider_uri": data.get("uri")}
        save_state(path, migrated)
        return migrated
    return _validate(data)
