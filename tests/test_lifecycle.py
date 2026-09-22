from __future__ import annotations

from dataclasses import dataclass

import pytest

from labctl.lifecycle import (
    CleanupError,
    LifecycleState,
    OperationRollbackError,
    ReconcileAction,
    ResetTransaction,
    StateOnlyRepairError,
    dependency_order,
    graceful_shutdown,
    reconcile,
    retry_cleanup,
    run_with_power_rollback,
    start_resources,
    stop_resources,
    transactional_create,
    transition,
)
from labctl.ssh import build_ssh_command, generate_keypair


@dataclass
class Resource:
    name: str
    dependencies: tuple[str, ...] = ()


def test_lifecycle_transitions_are_finite_and_validated() -> None:
    assert {state.value for state in LifecycleState} == {
        "provisioning",
        "ready",
        "running",
        "stopped",
        "degraded",
        "failed_cleanup",
    }
    assert transition(LifecycleState.PROVISIONING, LifecycleState.READY) is LifecycleState.READY
    assert transition(LifecycleState.RUNNING, LifecycleState.STOPPED) is LifecycleState.STOPPED
    with pytest.raises(ValueError, match="stopped -> provisioning"):
        transition(LifecycleState.STOPPED, LifecycleState.PROVISIONING)


def test_dependency_order_and_reverse_stop() -> None:
    resources = [Resource("app", ("network", "disk")), Resource("disk"), Resource("network")]
    assert [item.name for item in dependency_order(resources)] == ["network", "disk", "app"]
    assert [item.name for item in dependency_order(resources, reverse=True)] == [
        "app",
        "disk",
        "network",
    ]

    events: list[str] = []
    start_resources(resources, lambda resource: events.append(f"start:{resource.name}"))
    stop_resources(resources, lambda resource: events.append(f"stop:{resource.name}"))
    assert events == [
        "start:network",
        "start:disk",
        "start:app",
        "stop:app",
        "stop:disk",
        "stop:network",
    ]


def test_operation_failure_restores_pre_power_state_and_reports_all_errors() -> None:
    states = {"a": False, "b": True}

    def set_power(name: str, powered: bool) -> None:
        if name == "b" and powered:
            raise RuntimeError("restore b")
        states[name] = powered

    def operation() -> None:
        states.update(a=True, b=False)
        raise RuntimeError("start failed")

    with pytest.raises(OperationRollbackError) as caught:
        run_with_power_rollback(states, set_power, operation)

    assert str(caught.value.original) == "start failed"
    assert [str(error) for error in caught.value.restoration_errors] == ["restore b"]
    assert states["a"] is False


def test_graceful_shutdown_forces_destroy_only_after_timeout() -> None:
    calls: list[object] = []

    class Guest:
        def shutdown(self) -> None:
            calls.append("shutdown")

        def wait_stopped(self, timeout: float) -> bool:
            calls.append(("wait", timeout))
            return False

        def destroy(self) -> None:
            calls.append("destroy")

    assert graceful_shutdown(Guest(), 12.5) is False
    assert calls == ["shutdown", ("wait", 12.5), "destroy"]


def test_transactional_create_rolls_back_completed_steps_in_reverse() -> None:
    events: list[str] = []

    def fail() -> None:
        raise RuntimeError("create failed")

    with pytest.raises(OperationRollbackError) as caught:
        transactional_create(
            [
                (lambda: events.append("create-a"), lambda: events.append("remove-a")),
                (lambda: events.append("create-b"), lambda: events.append("remove-b")),
                (fail, lambda: events.append("remove-never")),
            ]
        )
    assert str(caught.value.original) == "create failed"
    assert events == ["create-a", "create-b", "remove-b", "remove-a"]


def test_reset_transaction_backup_commit_and_rollback() -> None:
    events: list[str] = []
    tx = ResetTransaction(
        backup=lambda: events.append("backup") or "snapshot",
        restore=lambda token: events.append(f"restore:{token}"),
        discard=lambda token: events.append(f"discard:{token}"),
    )
    tx.begin()
    tx.rollback()
    tx.begin()
    tx.commit()
    assert events == ["backup", "restore:snapshot", "backup", "discard:snapshot"]


def test_cleanup_retries_owned_resources_and_rejects_foreign_ones() -> None:
    attempts = 0

    def remove() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("busy")

    retry_cleanup(remove, actual_owner="lab-1", expected_owner="lab-1", attempts=3)
    assert attempts == 3
    with pytest.raises(CleanupError, match="ownership"):
        retry_cleanup(lambda: None, actual_owner="other", expected_owner="lab-1")


def test_reconcile_dry_run_and_state_only_resource_prohibition() -> None:
    actions = [ReconcileAction("record running", False), ReconcileAction("start vm", True)]
    applied: list[str] = []
    assert reconcile(actions, apply=applied.append, dry_run=True) == actions
    assert applied == []
    with pytest.raises(StateOnlyRepairError, match="start vm"):
        reconcile(actions, apply=applied.append, state_only=True)
    reconcile(actions[:1], apply=applied.append, state_only=True)
    assert applied == ["record running"]


def test_key_generation_and_strict_known_hosts_command(tmp_path) -> None:
    calls: list[list[str]] = []

    def run(args: list[str], *, check: bool) -> None:
        calls.append(args)
        (tmp_path / "id_lab").write_text("private")
        (tmp_path / "id_lab.pub").write_text("public")

    private, public = generate_keypair(tmp_path, runner=run)
    assert calls == [["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)]]
    assert private.stat().st_mode & 0o777 == 0o600
    assert public.stat().st_mode & 0o777 == 0o644

    command = build_ssh_command("student", "192.0.2.4", private, tmp_path / "known_hosts")
    assert command[:7] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={tmp_path / 'known_hosts'}",
    ]
    assert command[-1] == "student@192.0.2.4"
