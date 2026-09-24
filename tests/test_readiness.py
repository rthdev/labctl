"""Shared readiness regression tests: no libvirt or guest access."""

from pathlib import Path

import pytest

import labctl.orchestrator as module
from labctl.orchestrator import KVMOrchestrator, OrchestrationError
from labctl.subprocesses import CommandResult


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def readiness(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(module.time, "sleep", clock.sleep)
    vm = {
        "ssh_user": "student",
        "identity_file": str(tmp_path / "id_ed25519"),
        "known_hosts": str(tmp_path / "known_hosts"),
        "host_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest",
    }
    return clock, vm, tmp_path


class PermissionRunner:
    def __init__(self):
        self.calls = []

    def run(self, argv, *, check=True):
        self.calls.append(argv)
        if "domifaddr" in argv:
            return CommandResult(tuple(argv), 0, "192.0.2.10/24", "")
        if "cloud-init" in argv[-1]:
            privileged = "sudo -n" in argv[-1]
            return CommandResult(
                tuple(argv),
                0 if privileged else 1,
                "status: done" if privileged else "",
                "" if privileged else "Permission denied: /run/cloud-init/cloud.cfg",
            )
        return CommandResult(tuple(argv), 0, "", "")


def test_readiness_reads_root_only_cloud_init_status_with_pinned_ssh(readiness):
    _, vm, root = readiness
    runner = PermissionRunner()
    orchestrator = KVMOrchestrator(
        root / "data", root / "state", runner=runner, cloud_init_timeout=2
    )
    assert orchestrator._wait_ready("qemu:///system", "node", vm, None) == "192.0.2.10"
    ssh_calls = [call for call in runner.calls if "ssh" in call]
    assert len(ssh_calls) == 2
    for call in ssh_calls:
        assert "StrictHostKeyChecking=yes" in call
        assert "BatchMode=yes" in call
        assert f"UserKnownHostsFile={vm['known_hosts']}" in call
    assert "192.0.2.10 ssh-ed25519" in Path(vm["known_hosts"]).read_text()


@pytest.mark.parametrize(
    ("code", "stderr"),
    [
        pytest.param(1, "cloud-init failed: secret-stderr", id="cloud-init-fatal"),
        pytest.param(2, "cloud-init recoverable errors: secret-stderr", id="cloud-init-errors"),
        pytest.param(1, "sudo: a password is required; secret-stderr", id="sudo-refusal"),
        pytest.param(127, "cloud-init not found: secret-stderr", id="missing-command"),
    ],
)
def test_cloud_init_failure_is_immediate_and_does_not_leak_output(readiness, caplog, code, stderr):
    clock, vm, root = readiness

    class FailedStatus(PermissionRunner):
        def run(self, argv, *, check=True):
            result = super().run(argv, check=check)
            if "cloud-init" in argv[-1]:
                return CommandResult(tuple(argv), code, "secret-stdout", stderr)
            return result

    runner = FailedStatus()
    orchestrator = KVMOrchestrator(
        root / "data", root / "state", runner=runner, cloud_init_timeout=2
    )
    with caplog.at_level("DEBUG"), pytest.raises(OrchestrationError) as error:
        orchestrator._wait_ready("qemu:///system", "node", vm, None)
    assert "failed: cloud-init" in str(error.value)
    assert f"exit {code}" in str(error.value)
    assert "sudo" in str(error.value)
    assert clock.now == 0
    assert sum("cloud-init" in call[-1] for call in runner.calls) == 1
    assert "secret" not in str(error.value) + caplog.text


@pytest.mark.parametrize("phase", ["address", "SSH", "cloud-init"])
def test_every_readiness_invocation_and_retry_uses_remaining_budget(readiness, phase):
    clock, vm, root = readiness
    budgets = []

    class Unavailable(PermissionRunner):
        def run(self, argv, *, check=True):
            selected = (
                (phase == "address" and "domifaddr" in argv)
                or (phase == "SSH" and argv[-1] == "true")
                or (phase == "cloud-init" and "cloud-init" in argv[-1])
            )
            if selected:
                assert argv[:2] == ["timeout", "--signal=KILL"]
                budgets.append(float(argv[2]))
                clock.now += 0.25
                return CommandResult(tuple(argv), 255, "", "transport unavailable")
            return super().run(argv, check=check)

    orchestrator = KVMOrchestrator(
        root / "data",
        root / "state",
        runner=Unavailable(),
        address_timeout=1.75,
        ssh_timeout=1.75,
        cloud_init_timeout=1.75,
    )
    with pytest.raises(OrchestrationError, match=f"timed out: {phase} for node"):
        orchestrator._wait_ready("qemu:///system", "node", vm, None)
    assert budgets == [1.75, 0.5]
    assert clock.now == 1.75


@pytest.mark.parametrize("code", [-9, 124, 137, 255])
def test_transient_wait_or_transport_failure_can_recover(readiness, code):
    clock, vm, root = readiness
    budgets = []

    class Transient(PermissionRunner):
        def run(self, argv, *, check=True):
            if "cloud-init" in argv[-1]:
                budgets.append(float(argv[2]))
                if len(budgets) == 1:
                    clock.now += 0.25
                    return CommandResult(tuple(argv), code, "", "transient failure")
            return super().run(argv, check=check)

    orchestrator = KVMOrchestrator(
        root / "data", root / "state", runner=Transient(), cloud_init_timeout=2
    )
    assert orchestrator._wait_ready("qemu:///system", "node", vm, None) == "192.0.2.10"
    assert budgets == [2, 0.75]


@pytest.mark.parametrize("phase", ["address", "SSH", "cloud-init"])
def test_hung_readiness_subprocess_is_actually_killed(readiness, caplog, phase):
    import sys

    from labctl.subprocesses import Runner

    clock, vm, root = readiness
    results = []

    class Hung(PermissionRunner):
        def run(self, argv, *, check=True):
            selected = (
                (phase == "address" and "domifaddr" in argv)
                or (phase == "SSH" and argv[-1] == "true")
                or (phase == "cloud-init" and "cloud-init" in argv[-1])
            )
            if selected:
                # Exercise the real Runner and timeout executable, replacing only
                # the external service. A finite sleep prevents a broken wrapper
                # from hanging the suite. Ignore TERM to require a hard bound.
                command = [
                    *argv[:3],
                    sys.executable,
                    "-c",
                    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "print('sensitive-' + 'captured-output', flush=True); time.sleep(1)",
                ]
                result = Runner().run(command, check=False)
                results.append(result)
                clock.now += float(argv[2])
                return result
            return super().run(argv, check=check)

    orchestrator = KVMOrchestrator(
        root / "data",
        root / "state",
        runner=Hung(),
        address_timeout=0.1,
        ssh_timeout=0.1,
        cloud_init_timeout=0.1,
    )
    with caplog.at_level("DEBUG"), pytest.raises(OrchestrationError) as error:
        orchestrator._wait_ready("qemu:///system", "node", vm, None)
    assert f"timed out: {phase}" in str(error.value)
    assert len(results) == 1
    assert results[0].returncode in (137, -9)
    assert "sensitive-captured-output" not in str(error.value) + caplog.text


@pytest.mark.parametrize("phase", ["address", "SSH", "cloud-init"])
def test_zero_budget_never_invokes_phase_command(readiness, phase):
    _, vm, root = readiness
    runner = PermissionRunner()
    orchestrator = KVMOrchestrator(
        root / "data",
        root / "state",
        runner=runner,
        address_timeout=0 if phase == "address" else 1,
        ssh_timeout=0 if phase == "SSH" else 1,
        cloud_init_timeout=0 if phase == "cloud-init" else 1,
    )
    with pytest.raises(OrchestrationError, match=f"timed out: {phase}"):
        orchestrator._wait_ready("qemu:///system", "node", vm, None)
    assert len(runner.calls) == {"address": 0, "SSH": 1, "cloud-init": 2}[phase]
