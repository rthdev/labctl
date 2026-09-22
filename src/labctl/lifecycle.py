"""Backend-independent lifecycle and recovery primitives."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast


class LifecycleState(StrEnum):
    """Persisted lifecycle states."""

    PROVISIONING = "provisioning"
    READY = "ready"
    RUNNING = "running"
    STOPPED = "stopped"
    DEGRADED = "degraded"
    FAILED_CLEANUP = "failed_cleanup"


_TRANSITIONS = {
    LifecycleState.PROVISIONING: {
        LifecycleState.READY,
        LifecycleState.DEGRADED,
        LifecycleState.FAILED_CLEANUP,
    },
    LifecycleState.READY: {
        LifecycleState.RUNNING,
        LifecycleState.STOPPED,
        LifecycleState.DEGRADED,
        LifecycleState.FAILED_CLEANUP,
    },
    LifecycleState.RUNNING: {
        LifecycleState.STOPPED,
        LifecycleState.DEGRADED,
        LifecycleState.FAILED_CLEANUP,
    },
    LifecycleState.STOPPED: {
        LifecycleState.RUNNING,
        LifecycleState.DEGRADED,
        LifecycleState.FAILED_CLEANUP,
    },
    LifecycleState.DEGRADED: {
        LifecycleState.READY,
        LifecycleState.RUNNING,
        LifecycleState.STOPPED,
        LifecycleState.FAILED_CLEANUP,
    },
    LifecycleState.FAILED_CLEANUP: {
        LifecycleState.DEGRADED,
    },
}


def transition(current: LifecycleState, target: LifecycleState) -> LifecycleState:
    """Validate and return a lifecycle state transition."""
    if target is current:
        return target
    if target not in _TRANSITIONS[current]:
        raise ValueError(f"invalid lifecycle transition: {current.value} -> {target.value}")
    return target


class DependentResource(Protocol):
    """Minimal resource shape needed for dependency sorting."""

    name: str
    dependencies: tuple[str, ...]


def dependency_order[ResourceT: DependentResource](
    resources: Iterable[ResourceT], *, reverse: bool = False
) -> list[ResourceT]:
    """Return a stable topological order, optionally suitable for stopping."""
    items = list(resources)
    by_name = {item.name: item for item in items}
    if len(by_name) != len(items):
        raise ValueError("duplicate resource name")
    result: list[ResourceT] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item: ResourceT) -> None:
        if item.name in visiting:
            raise ValueError(f"dependency cycle at {item.name}")
        if item.name in visited:
            return
        visiting.add(item.name)
        for dependency in item.dependencies:
            if dependency not in by_name:
                raise ValueError(f"unknown dependency {dependency} for {item.name}")
            visit(by_name[dependency])
        visiting.remove(item.name)
        visited.add(item.name)
        result.append(item)

    for resource in items:
        visit(resource)
    if reverse:
        result.reverse()
    return result


def start_resources[ResourceT: DependentResource](
    resources: Iterable[ResourceT], start: Callable[[ResourceT], None]
) -> None:
    """Start resources only after their dependencies."""
    for resource in dependency_order(resources):
        start(resource)


def stop_resources[ResourceT: DependentResource](
    resources: Iterable[ResourceT], stop: Callable[[ResourceT], None]
) -> None:
    """Stop dependants before the resources they require."""
    for resource in dependency_order(resources, reverse=True):
        stop(resource)


class OperationRollbackError(RuntimeError):
    """An operation failed and may also have failed during restoration."""

    def __init__(self, original: Exception, restoration_errors: Sequence[Exception] = ()) -> None:
        self.original = original
        self.restoration_errors = tuple(restoration_errors)
        suffix = ""
        if self.restoration_errors:
            suffix = "; restoration errors: " + "; ".join(map(str, self.restoration_errors))
        super().__init__(f"operation failed: {original}{suffix}")


def run_with_power_rollback(
    power_states: Mapping[str, bool],
    set_power: Callable[[str, bool], None],
    operation: Callable[[], None],
) -> None:
    """Run an operation and restore every changed power state if it fails."""
    before = dict(power_states)
    try:
        operation()
    except Exception as original:
        restoration_errors: list[Exception] = []
        for name, powered in reversed(before.items()):
            if power_states.get(name) == powered:
                continue
            try:
                set_power(name, powered)
            except Exception as error:
                restoration_errors.append(error)
        raise OperationRollbackError(original, restoration_errors) from original


class ShutdownGuest(Protocol):
    def shutdown(self) -> None: ...

    def wait_stopped(self, timeout: float) -> bool: ...

    def destroy(self) -> None: ...


def graceful_shutdown(guest: ShutdownGuest, timeout: float) -> bool:
    """Request shutdown, force destruction after timeout, and report gracefulness."""
    if timeout < 0:
        raise ValueError("timeout must be non-negative")
    guest.shutdown()
    if guest.wait_stopped(timeout):
        return True
    guest.destroy()
    return False


CreateStep = tuple[Callable[[], None], Callable[[], None]]


def transactional_create(steps: Iterable[CreateStep]) -> None:
    """Run create steps and undo completed steps in reverse on failure."""
    rollbacks: list[Callable[[], None]] = []
    try:
        for create, rollback in steps:
            create()
            rollbacks.append(rollback)
    except Exception as original:
        restoration_errors: list[Exception] = []
        for rollback in reversed(rollbacks):
            try:
                rollback()
            except Exception as error:
                restoration_errors.append(error)
        raise OperationRollbackError(original, restoration_errors) from original


_MISSING = object()


class ResetTransaction[BackupT]:
    """Own one reset backup until explicit commit or rollback."""

    def __init__(
        self,
        *,
        backup: Callable[[], BackupT],
        restore: Callable[[BackupT], None],
        discard: Callable[[BackupT], None],
    ) -> None:
        self._backup = backup
        self._restore = restore
        self._discard = discard
        self._token: BackupT | object = _MISSING
        self._active = False

    def begin(self) -> None:
        if self._active:
            raise RuntimeError("reset transaction already active")
        self._token = self._backup()
        self._active = True

    def rollback(self) -> None:
        token = self._take_token()
        self._restore(token)

    def commit(self) -> None:
        token = self._take_token()
        self._discard(token)

    def _take_token(self) -> BackupT:
        if not self._active:
            raise RuntimeError("reset transaction is not active")
        self._active = False
        token = self._token
        self._token = _MISSING
        return cast("BackupT", token)


class CleanupError(RuntimeError):
    """A resource could not safely be cleaned up."""


def retry_cleanup(
    remove: Callable[[], None],
    *,
    actual_owner: str | None,
    expected_owner: str,
    attempts: int = 3,
) -> None:
    """Remove an owned resource, retrying transient backend failures."""
    if actual_owner != expected_owner:
        raise CleanupError("resource ownership does not match requested lab")
    if attempts < 1:
        raise ValueError("attempts must be positive")
    errors: list[Exception] = []
    for _ in range(attempts):
        try:
            remove()
            return
        except Exception as error:
            errors.append(error)
    raise CleanupError(f"cleanup failed after {attempts} attempts: {errors[-1]}") from errors[-1]


@dataclass(frozen=True)
class ReconcileAction:
    description: str
    changes_resource: bool


class StateOnlyRepairError(RuntimeError):
    """A state-only reconcile attempted to mutate a resource."""


def reconcile(
    actions: Sequence[ReconcileAction],
    *,
    apply: Callable[[str], None],
    dry_run: bool = False,
    state_only: bool = False,
) -> Sequence[ReconcileAction]:
    """Validate and optionally apply a precomputed reconciliation plan."""
    if state_only:
        forbidden = next((action for action in actions if action.changes_resource), None)
        if forbidden is not None:
            raise StateOnlyRepairError(
                f"state-only repair cannot change resources: {forbidden.description}"
            )
    if not dry_run:
        for action in actions:
            apply(action.description)
    return actions
