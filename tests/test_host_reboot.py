"""Host-reboot lifecycle regressions; simulated libvirt, never live guests."""

from pathlib import Path

import pytest
from test_application import DriftedOrchestrator
from test_orchestrator import ResetFileRunner, StateRunner, _single_reset_fixture, _vm, _write_state

from labctl.application import Application
from labctl.cli import parse_args
from labctl.errors import LabctlError
from labctl.orchestrator import KVMOrchestrator, OrchestrationError
from labctl.state import load_state
from labctl.subprocesses import CommandResult


class RebootRunner(StateRunner):
    def __init__(self, power: dict[str, str], network_xml: str) -> None:
        super().__init__(power, network_xml=network_xml)
        self.network_active = False
        self.network_exists = True
        self.network_autostart = False
        self.domains = set(power)

    def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
        command = argv[3] if argv[0] == "virsh" else argv[0]
        if command == "dominfo" and argv[4] not in self.domains:
            self.calls.append(tuple(argv))
            return CommandResult(tuple(argv), 1, "", f"failed to get domain '{argv[4]}'")
        if command == "undefine":
            self.domains.discard(argv[4])
        if command == "virt-install":
            self.domains.add(argv[argv.index("--name") + 1])
        if command == "net-autostart":
            self.network_autostart = True
        if command == "net-undefine":
            self.network_exists = False
            self.network_autostart = False
        if command == "net-info":
            self.calls.append(tuple(argv))
            if not self.network_exists:
                return CommandResult(tuple(argv), 1, "", f"failed to get network '{argv[4]}'")
            active = "yes" if self.network_active else "no"
            return CommandResult(tuple(argv), 0, f"Active: {active}\nPersistent: yes\n", "")
        if command == "net-start":
            self.network_active = True
        if command in {"start", "virt-install"} and not self.network_active:
            raise RuntimeError("network is not active")
        return super().run(argv, check=check)


def test_explicit_start_recovers_owned_network_before_guest(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    _write_state(tmp_path, {"node": _vm(domain, tmp_path, "ready")})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state")
    runner = RebootRunner(
        {domain: "shut off"},
        orchestrator._network_xml("labctl-u1000-lx001-network", "u1000-lx001"),
    )
    orchestrator.runner = runner
    state = orchestrator.start("LX001", readiness_probe=lambda *_: True)
    assert state["state"] == "ready"
    assert state["vms"]["node"]["address"] == "192.0.2.10"
    commands = [call[3] for call in runner.calls]
    assert commands.index("net-start") < commands.index("start")
    assert commands.count("net-start") == 1
    assert "autostart" not in commands
    runner.calls.clear()
    orchestrator.start("LX001", readiness_probe=lambda *_: True)
    assert not any(call[3] in {"net-start", "start"} for call in runner.calls)


@pytest.mark.parametrize("active", [False, True])
def test_explicit_resume_enables_only_owned_network_autostart(tmp_path: Path, active: bool):
    domain = "labctl-u1000-lx001-node"
    _write_state(tmp_path, {"node": _vm(domain, tmp_path, "ready")})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state")
    runner = RebootRunner(
        {domain: "shut off"}, orchestrator._network_xml("labctl-u1000-lx001-network", "u1000-lx001")
    )
    runner.network_active = active
    orchestrator.runner = runner
    orchestrator.start("LX001", readiness_probe=lambda *_: True)
    commands = [call[3] for call in runner.calls]
    assert commands.count("net-autostart") == 1
    assert commands.index("net-autostart") < commands.index("start")
    assert commands.count("net-start") == (0 if active else 1)
    assert "autostart" not in commands
    assert all(
        call[4] == "labctl-u1000-lx001-network"
        for call in runner.calls
        if call[3] == "net-autostart"
    )


@pytest.mark.parametrize("peer_saved", ["ready", "stopped"])
def test_selective_resume_leaves_offline_peer_stopped(tmp_path: Path, peer_saved: str) -> None:
    domains = {name: f"labctl-u1000-lx001-{name}" for name in ("node", "peer")}
    _write_state(
        tmp_path,
        {
            "node": _vm(domains["node"], tmp_path, "ready"),
            "peer": _vm(domains["peer"], tmp_path, peer_saved),
        },
    )
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state")
    runner = RebootRunner(
        {domain: "shut off" for domain in domains.values()},
        orchestrator._network_xml("labctl-u1000-lx001-network", "u1000-lx001"),
    )
    orchestrator.runner = runner
    state = orchestrator.start("LX001", vm_name="node", readiness_probe=lambda *_: True)
    assert state["state"] == "degraded"
    assert state["vms"]["peer"]["state"] == "stopped"
    assert runner.power[domains["peer"]] == "shut off"
    assert [call[4] for call in runner.calls if call[3] == "start"] == [domains["node"]]


class RebootResetRunner(RebootRunner, ResetFileRunner):
    pass


@pytest.mark.parametrize("selection", [None, ("node",)])
def test_reset_after_reboot_activates_network_before_disk_mutation(tmp_path: Path, selection):
    domain, overlay, images, _ = _single_reset_fixture(tmp_path)
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state")
    runner = RebootResetRunner(
        {domain: "shut off"},
        orchestrator._network_xml("labctl-u1000-lx001-network", "u1000-lx001"),
    )
    orchestrator.runner = runner
    state = orchestrator.reset("LX001", images, vm_names=selection, readiness_probe=lambda *_: True)
    assert state["state"] == "ready"
    assert overlay.read_bytes() == b"replacement overlay"
    commands = [call[3] if call[0] == "virsh" else call[0] for call in runner.calls]
    assert commands.index("net-start") < commands.index("undefine")
    assert not orchestrator._journal_path("LX001").exists()


@pytest.mark.parametrize("operation", ["start", "reset"])
@pytest.mark.parametrize(
    "fault", ["foreign", "configuration", "missing", "indeterminate", "no-op", "autostart-failure"]
)
def test_network_recovery_fails_closed_before_guest_mutation(tmp_path, operation, fault):
    domain, overlay, images, _ = _single_reset_fixture(tmp_path)
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state")
    xml = orchestrator._network_xml("labctl-u1000-lx001-network", "u1000-lx001")
    if fault == "foreign":
        xml = xml.replace(">u1000-lx001<", ">foreign<")
        assert ">foreign<" in xml
    elif fault == "configuration":
        xml = xml.replace('mode="nat"', 'mode="route"')

    class FaultRunner(RebootResetRunner):
        def run(self, argv, *, check=True):
            if fault == "autostart-failure" and "net-autostart" in argv:
                self.calls.append(tuple(argv))
                raise RuntimeError("net-autostart failed")
            if argv[0] == "virsh" and (
                (fault == "indeterminate" and argv[3] == "net-info")
                or (fault == "no-op" and argv[3] == "net-start")
            ):
                self.calls.append(tuple(argv))
                return CommandResult(tuple(argv), 0, "", "")
            return super().run(argv, check=check)

    runner = FaultRunner({domain: "shut off"}, xml)
    if fault == "missing":
        runner.network_exists = False
    orchestrator.runner = runner
    original = orchestrator._state_path("LX001").read_bytes()
    with pytest.raises((OrchestrationError, RuntimeError)):
        if operation == "start":
            orchestrator.start("LX001", readiness_probe=lambda *_: True)
        else:
            orchestrator.reset("LX001", images, readiness_probe=lambda *_: True)
    assert overlay.read_bytes() == b"original overlay"
    assert orchestrator._state_path("LX001").read_bytes() == original
    assert not orchestrator._journal_path("LX001").exists()
    assert not any(call[3] in {"start", "destroy", "undefine", "shutdown"} for call in runner.calls)
    if fault != "no-op":
        assert not any(call[3] == "net-start" for call in runner.calls)


def test_active_network_drift_refused_before_enabling_autostart(tmp_path: Path):
    domain = "labctl-u1000-lx001-node"
    _write_state(tmp_path, {"node": _vm(domain, tmp_path, "ready")})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state")
    xml = orchestrator._network_xml("labctl-u1000-lx001-network", "u1000-lx001")
    runner = RebootRunner({domain: "shut off"}, xml.replace('mode="nat"', 'mode="route"'))
    runner.network_active = True
    orchestrator.runner = runner
    with pytest.raises(OrchestrationError, match="network configuration drift"):
        orchestrator.start("LX001", readiness_probe=lambda *_: True)
    assert not any(call[3] in {"net-autostart", "net-start", "start"} for call in runner.calls)


@pytest.mark.parametrize("fault", ["ownership", "configuration"])
def test_persistent_network_drift_refused_before_autostart(tmp_path: Path, fault: str):
    domain = "labctl-u1000-lx001-node"
    _write_state(tmp_path, {"node": _vm(domain, tmp_path, "ready")})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state")
    xml = orchestrator._network_xml("labctl-u1000-lx001-network", "u1000-lx001")
    changed = (
        xml.replace(">u1000-lx001<", ">foreign<")
        if fault == "ownership"
        else xml.replace('mode="nat"', 'mode="route"')
    )

    class PersistentDriftRunner(RebootRunner):
        def run(self, argv, *, check=True):
            if "net-dumpxml" in argv and "--inactive" in argv:
                self.calls.append(tuple(argv))
                return CommandResult(tuple(argv), 0, changed, "")
            return super().run(argv, check=check)

    runner = PersistentDriftRunner({domain: "shut off"}, xml)
    runner.network_active = True
    orchestrator.runner = runner
    with pytest.raises(OrchestrationError, match=r"network.*drift|ownership verification"):
        orchestrator.start("LX001", readiness_probe=lambda *_: True)
    assert not any(call[3] in {"net-autostart", "net-start", "start"} for call in runner.calls)


def _cli_app(tmp_path, monkeypatch, orchestrator):
    app = Application(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        runtime_root=tmp_path / "runtime",
    )
    monkeypatch.setattr(app, "_orchestrator", lambda _: orchestrator)
    # Substitute guest readiness only, not lifecycle or application dispatch.
    monkeypatch.setattr(orchestrator, "_wait_ready", lambda *_: "192.0.2.77")
    return app


@pytest.mark.parametrize("scope", ["lab", "vm"])
def test_cli_explicit_resume_preserves_disk_and_refreshes_address(tmp_path, monkeypatch, scope):
    domain, overlay, _, _ = _single_reset_fixture(tmp_path)
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state")
    runner = RebootRunner(
        {domain: "shut off"}, orchestrator._network_xml("labctl-u1000-lx001-network", "u1000-lx001")
    )
    orchestrator.runner = runner
    app = _cli_app(tmp_path, monkeypatch, orchestrator)
    args = [scope, "start", "LX001"] + (["node"] if scope == "vm" else [])
    app.execute(parse_args(args))
    state = load_state(orchestrator._state_path("LX001"))
    assert state["vms"]["node"]["state"] == "ready"
    assert state["vms"]["node"]["address"] == "192.0.2.77"
    assert overlay.read_bytes() == b"original overlay"
    assert runner.network_active and runner.power[domain] == "running"


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("running", [False, True])
def test_cli_remove_observes_live_power_after_reboot(tmp_path, monkeypatch, force, running):
    domain, overlay, _, _ = _single_reset_fixture(tmp_path)
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state")
    runner = RebootRunner(
        {domain: "running" if running else "shut off"},
        orchestrator._network_xml("labctl-u1000-lx001-network", "u1000-lx001"),
    )
    runner.network_active = running
    runner.network_autostart = True
    orchestrator.runner = runner
    app = _cli_app(tmp_path, monkeypatch, orchestrator)
    args = parse_args(["lab", "rm", "LX001"] + (["--force"] if force else []))
    if running and not force:
        with pytest.raises(LabctlError, match="lab is running"):
            app.execute(args)
        assert overlay.exists() and domain in runner.domains
    else:
        app.execute(args)
        assert not overlay.exists()
        assert not orchestrator._state_path("LX001").exists()
        assert not orchestrator._journal_path("LX001").exists()
        assert not runner.domains and not runner.network_exists and not runner.network_autostart
    assert not any(
        call[3] in {"start", "net-start", "net-define", "net-autostart"} for call in runner.calls
    )


@pytest.mark.parametrize("operation", ["stop", "restart"])
def test_offline_stop_restart_do_not_boot_guests_or_network(tmp_path: Path, operation: str):
    domain, overlay, _, _ = _single_reset_fixture(tmp_path)
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state")
    runner = RebootRunner(
        {domain: "shut off"}, orchestrator._network_xml("labctl-u1000-lx001-network", "u1000-lx001")
    )
    orchestrator.runner = runner
    state = getattr(orchestrator, operation)("LX001", force=False)
    assert state["state"] == "stopped"
    assert overlay.read_bytes() == b"original overlay"
    assert not runner.network_active
    assert not any(
        call[3] in {"start", "net-start", "shutdown", "destroy"} for call in runner.calls
    )


@pytest.mark.parametrize("command", [["start"], ["rm"], ["rm", "--force"]])
def test_cli_foreign_domain_after_reboot_is_never_recovered(tmp_path, monkeypatch, command):
    domain, overlay, _, _ = _single_reset_fixture(tmp_path)
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state")

    class ForeignRunner(RebootRunner):
        def run(self, argv, *, check=True):
            result = super().run(argv, check=check)
            if argv[3] == "metadata":
                return CommandResult(
                    result.argv, 0, result.stdout.replace("u1000-lx001", "foreign"), ""
                )
            return result

    runner = ForeignRunner(
        {domain: "shut off"}, orchestrator._network_xml("labctl-u1000-lx001-network", "u1000-lx001")
    )
    orchestrator.runner = runner
    app = _cli_app(tmp_path, monkeypatch, orchestrator)
    with pytest.raises(LabctlError, match="ownership verification failed"):
        app.execute(parse_args(["lab", command[0], "LX001", *command[1:]]))
    assert overlay.read_bytes() == b"original overlay"
    assert not any(
        call[3] in {"start", "net-start", "undefine", "destroy"} for call in runner.calls
    )


@pytest.mark.parametrize("command", [["start"], ["rm"], ["rm", "--force"]])
def test_cli_reboot_does_not_bypass_pending_reset(tmp_path, monkeypatch, command):
    domain, overlay, _, _ = _single_reset_fixture(tmp_path)
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state")
    runner = RebootRunner(
        {domain: "shut off"}, orchestrator._network_xml("labctl-u1000-lx001-network", "u1000-lx001")
    )
    orchestrator.runner = runner
    app = _cli_app(tmp_path, monkeypatch, orchestrator)
    journal = orchestrator._journal_path("LX001")
    journal.parent.mkdir(parents=True, exist_ok=True)
    content = '{"schema_version":1,"id":"LX001","operation":"reset"}'
    journal.write_text(content)
    with pytest.raises(LabctlError, match=r"pending transaction \(reset\)"):
        app.execute(parse_args(["lab", command[0], "LX001", *command[1:]]))
    assert runner.calls == []
    assert journal.read_text() == content
    assert overlay.read_bytes() == b"original overlay"


def test_failed_resume_restores_guests_without_destroying_shared_network(tmp_path: Path):
    domains = {name: f"labctl-u1000-lx001-{name}" for name in ("first", "second")}
    path = _write_state(
        tmp_path, {name: _vm(domain, tmp_path, "ready") for name, domain in domains.items()}
    )
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state")
    runner = RebootRunner(
        {domain: "shut off" for domain in domains.values()},
        orchestrator._network_xml("labctl-u1000-lx001-network", "u1000-lx001"),
    )
    runner.fail_start = domains["second"]
    orchestrator.runner = runner
    with pytest.raises(OrchestrationError, match="failed start"):
        orchestrator.start("LX001", readiness_probe=lambda *_: True)
    assert all(power == "shut off" for power in runner.power.values())
    assert load_state(path)["state"] == "stopped"
    assert runner.network_active
    assert not any(call[3] in {"net-destroy", "net-undefine"} for call in runner.calls)


@pytest.mark.parametrize("material_drift", [False, True])
def test_grade_after_host_shutdown_gives_explicit_resume_guidance(
    tmp_path, monkeypatch, material_drift
):
    class OfflineOrchestrator(DriftedOrchestrator):
        def reconcile(self, *, repair):
            assert not repair
            actions = [
                {"resource": "node", "action": "mark-stopped", "reason": "domain is stopped"}
            ]
            return actions + (super().reconcile(repair=repair) if material_drift else [])

    app = Application(data_root=tmp_path / "data", state_root=tmp_path / "state")
    monkeypatch.setattr(app, "_orchestrator", lambda _: OfflineOrchestrator())
    match = "resource drift" if material_drift else "not running; use labctl lab start LX001"
    with pytest.raises(LabctlError, match=match):
        app.execute(parse_args(["lab", "grade", "LX001"]))
