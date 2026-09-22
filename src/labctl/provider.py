"""Versioned provider interface, common models, and explicit plugin discovery."""

from __future__ import annotations

import importlib
import importlib.util
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import ModuleType
from typing import Any

PROVIDER_API_VERSION = 1


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Provider facilities needed for a complete lab."""

    compute: bool
    storage: bool
    network: bool

    @property
    def complete(self) -> bool:
        return self.compute and self.storage and self.network


class HealthStatus(StrEnum):
    PASS = "pass"  # noqa: S105 - health state, not a password
    WARNING = "warning"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class HealthCheck:
    name: str
    status: HealthStatus
    detail: str
    guidance: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    checks: tuple[HealthCheck, ...]

    @property
    def healthy(self) -> bool:
        return all(check.status is not HealthStatus.FAIL for check in self.checks)

    @property
    def warnings(self) -> tuple[HealthCheck, ...]:
        return tuple(check for check in self.checks if check.status is HealthStatus.WARNING)


@dataclass(frozen=True, slots=True)
class Ownership:
    provider_id: str
    uid: str
    resource_type: str

    @property
    def metadata(self) -> dict[str, str]:
        return {
            "labctl.provider": self.provider_id,
            "labctl.uid": self.uid,
            "labctl.resource-type": self.resource_type,
        }


class ReconcileAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    NONE = "none"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class Reconciliation:
    action: ReconcileAction
    reason: str


class Provider(ABC):
    """Stable provider API implemented by built-in and external providers."""

    api_version = PROVIDER_API_VERSION
    id: str

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Return currently usable capabilities."""

    @abstractmethod
    def doctor(self) -> ProviderHealth:
        """Diagnose host prerequisites without changing the host."""

    @abstractmethod
    def reconcile(
        self, desired: Ownership, observed_metadata: Mapping[str, str] | None
    ) -> Reconciliation:
        """Classify the safe operation for an owned resource."""


class ProviderDiscoveryError(RuntimeError):
    """An explicitly enabled provider module is unusable."""


def _provider_from_module(module: ModuleType, name: str) -> Provider:
    candidate: Any = getattr(module, "PROVIDER", None)
    if not isinstance(candidate, Provider):
        raise ProviderDiscoveryError(f"provider module {name!r} must expose a Provider as PROVIDER")
    version: Any = getattr(module, "PROVIDER_API_VERSION", None)
    if version != PROVIDER_API_VERSION:
        raise ProviderDiscoveryError(
            f"provider module {name!r} API version {version!r} does not match "
            f"{PROVIDER_API_VERSION}"
        )
    if candidate.api_version != PROVIDER_API_VERSION:
        raise ProviderDiscoveryError(
            f"provider {candidate.id!r} API version {candidate.api_version!r} does not match "
            f"{PROVIDER_API_VERSION}"
        )
    if not candidate.id or not isinstance(candidate.id, str):
        raise ProviderDiscoveryError(f"provider module {name!r} has an invalid provider ID")
    return candidate


def discover_providers(
    enabled_modules: Iterable[str], *, search_paths: Iterable[str | Path] = ()
) -> dict[str, Provider]:
    """Import and validate only the module names explicitly enabled by configuration."""

    providers: dict[str, Provider] = {}
    directories = [Path(path) for path in search_paths]
    for name in enabled_modules:
        if not name or not isinstance(name, str):
            raise ProviderDiscoveryError("enabled provider module names must be non-empty strings")
        try:
            module = None
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                for directory in directories:
                    candidate = directory / f"{name}.py"
                    if not candidate.is_file() or candidate.is_symlink():
                        continue
                    specification = importlib.util.spec_from_file_location(
                        f"_labctl_provider_{name}", candidate
                    )
                    if specification is None or specification.loader is None:
                        raise ProviderDiscoveryError(
                            f"provider module {name!r} could not load from {candidate}"
                        )
                    module = importlib.util.module_from_spec(specification)
                    specification.loader.exec_module(module)
                    break
            if module is None:
                module = importlib.import_module(name)
        except Exception as exc:
            raise ProviderDiscoveryError(
                f"provider module {name!r} could not import: {exc}"
            ) from exc
        provider = _provider_from_module(module, name)
        if provider.id in providers:
            raise ProviderDiscoveryError(f"duplicate provider ID {provider.id!r}")
        providers[provider.id] = provider
    return providers
