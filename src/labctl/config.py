"""Strict TOML configuration with CLI, environment, file, default precedence."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Config:
    provider: str = "kvm"
    libvirt_uri: str | None = None
    libvirt_storage_root: str = field(
        default_factory=lambda: f"/var/lib/libvirt/images/labctl/{os.getuid()}"
    )
    shutdown_timeout: int = 60
    address_timeout: int = 120
    ssh_timeout: int = 180
    cloud_init_timeout: int = 600
    grader_timeout: int = 120
    enabled_providers: tuple[str, ...] = ()
    provider_paths: tuple[str, ...] = ()
    definition_paths: tuple[str, ...] = ()
    allow_definition_override: bool = False
    log_level: str = "warning"


_ENV = {f"LABCTL_{field.name.upper()}": field.name for field in fields(Config)}


def _convert(name: str, value: Any) -> Any:
    if name == "libvirt_storage_root":
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ConfigError(f"{name} must be an absolute path")
        return value
    if name in {
        "shutdown_timeout",
        "address_timeout",
        "ssh_timeout",
        "cloud_init_timeout",
        "grader_timeout",
    }:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{name} must be an integer") from exc
        if result <= 0:
            raise ConfigError(f"{name} must be positive")
        return result
    if name == "allow_definition_override":
        if isinstance(value, bool):
            return value
        values = {"true": True, "false": False, "1": True, "0": False}
        try:
            return values[str(value).lower()]
        except KeyError as exc:
            raise ConfigError(f"{name} must be a boolean") from exc
    if name in {"enabled_providers", "provider_paths", "definition_paths"}:
        if isinstance(value, str):
            return tuple(part for part in value.split(os.pathsep) if part)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return tuple(value)
        raise ConfigError(f"{name} must be a string list")
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    return value


def load_config(
    path: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    cli: Mapping[str, Any] | None = None,
) -> Config:
    allowed = {field.name for field in fields(Config)}
    data: dict[str, Any] = {}
    if path is not None and path.exists():
        try:
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read configuration {path}: {exc}") from exc
        unknown = parsed.keys() - allowed
        if unknown:
            raise ConfigError(f"unknown configuration setting: {sorted(unknown)[0]}")
        data.update(parsed)
    environment = os.environ if env is None else env
    for variable, name in _ENV.items():
        if variable in environment:
            data[name] = environment[variable]
    for name, value in (cli or {}).items():
        if name not in allowed:
            raise ConfigError(f"unknown CLI configuration setting: {name}")
        if value is not None:
            data[name] = value
    converted = {name: _convert(name, value) for name, value in data.items()}
    return Config(**converted)
