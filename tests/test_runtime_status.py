"""Live status is a read-only observation, never saved readiness."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from labctl import runtime
from labctl.application import Application
from labctl.cli import parse_args


class Libvirt:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.states = {"labctl-owned-node": 5}

    def run(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(argv)
        assert kwargs["timeout"] <= 3

        assert kwargs["env"]["LC_ALL"] == "C"
        assert "--readonly" in argv
        command = argv[4]
        if command == "domstats":
            output = "\n".join(
                f"Domain: '{name}'\n  state.state={value}\n  state.reason=0\n"
                for name, value in self.states.items()
            )
        elif command == "dumpxml":
            output = f"""<domain><name>{argv[5]}</name><uuid>test-uuid</uuid>
<metadata><ownership xmlns="urn:labctl"><provider>kvm</provider><uid>owned</uid>
<resource-type>domain</resource-type><vm-name>{argv[5].removeprefix("labctl-owned-")}</vm-name></ownership></metadata>
</domain>"""
        else:
            pytest.fail(f"unexpected (possibly mutating) command: {argv}")
        return subprocess.CompletedProcess(argv, 0, output, "")


def fixture_app(tmp_path: Path) -> tuple[Application, Path]:
    path = tmp_path / "state/labs/LX001.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "LX001",
                "provider": "kvm",
                "provider_uri": "qemu:///system",
                "instance_uid": "owned",
                "state": "ready",
                "vms": {"node": {"domain": "labctl-owned-node", "state": "ready"}},
            }
        )
    )
    return Application(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        runtime_root=tmp_path / "runtime",
    ), path


def test_reboot_listing_observes_stopped_without_rewriting_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, path = fixture_app(tmp_path)
    before = path.read_bytes()
    libvirt = Libvirt()
    monkeypatch.setattr(runtime, "_run_command", libvirt.run)
    result = app.execute(parse_args(["vm", "ls"]))
    assert result.data[0]["state"] == "stopped"
    assert result.data[0]["recorded_state"] == "ready"
    assert result.data[0]["observation"]["state"] == "stopped"
    assert path.read_bytes() == before
    assert len(libvirt.calls) == 2


@pytest.mark.parametrize(
    "power,expected",
    [
        (1, "running"),
        (2, "blocked"),
        (3, "paused"),
        (4, "shutdown"),
        (5, "stopped"),
        (6, "crashed"),
        (7, "pmsuspended"),
        (0, "unknown"),
        (99, "unknown"),
        (None, "missing"),
    ],
)
def test_vm_power_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, power: int | None, expected: str
) -> None:
    app, _ = fixture_app(tmp_path)
    libvirt = Libvirt()
    libvirt.states = {} if power is None else {"labctl-owned-node": power}
    monkeypatch.setattr(runtime, "_run_command", libvirt.run)
    row = app.execute(parse_args(["vms"])).data[0]
    assert row["state"] == expected
    assert row["recorded_state"] == "ready"
    assert len(libvirt.calls) == (1 if power is None else 2)
    if expected in {"unknown", "missing"}:
        assert row["observation"]["diagnostics"]


@pytest.mark.parametrize(
    "failure",
    [
        "unavailable",
        "permission",
        "timeout",
        "missing-binary",
        "ownership",
        "malformed",
        "xml",
        "identity",
        "unsupported",
        "missing-uri",
        "missing-provider",
    ],
)
def test_observation_failures_are_unknown_not_cached_or_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    app, path = fixture_app(tmp_path)
    state = json.loads(path.read_text())
    if failure == "unsupported":
        state["provider"] = "external"
    if failure == "missing-uri":
        state["provider_uri"] = ""
    if failure == "identity":
        state.pop("instance_uid")
    if failure == "missing-provider":
        state.pop("provider")
    path.write_text(json.dumps(state))
    libvirt = Libvirt()

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]
        assert failure not in {"unsupported", "missing-uri", "identity", "missing-provider"}
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 3, stderr="SECRET")
        if failure == "missing-binary":
            raise FileNotFoundError("SECRET")
        if failure in {"unavailable", "permission"}:
            return subprocess.CompletedProcess(argv, 1, "", f"{failure} SECRET")
        result = libvirt.run(argv, **kwargs)
        if failure == "malformed" and argv[4] == "domstats":
            result.stdout = "Domain: 'labctl-owned-node'\n invalid SECRET"
        if argv[4] == "dumpxml":
            if failure == "ownership":
                result.stdout = result.stdout.replace("<uid>owned", "<uid>foreign")
            if failure == "xml":
                result.stdout = "invalid SECRET"
        return result

    monkeypatch.setattr(runtime, "_run_command", run)
    result = app.execute(parse_args(["vm", "list"]))
    row = result.data[0]
    assert row["state"] == "unknown"
    assert row["recorded_state"] == "ready"
    assert row["observation"]["diagnostics"]
    assert "SECRET" not in json.dumps(result.data)
    assert json.loads(path.read_text()) == state


@pytest.mark.parametrize(
    "command",
    [
        ["labs", "--active"],
        ["lab", "ls", "--active"],
        ["lab", "list", "--active"],
        ["lab", "inspect", "LX001"],
    ],
)
@pytest.mark.parametrize(
    "powers,aggregate",
    [
        ((1, 1), "running"),
        ((5, 5), "stopped"),
        ((1, 5), "partial"),
        ((1, 3), "degraded"),
        ((1, None), "degraded"),
        ((0, 5), "unknown"),
    ],
)
def test_lab_aggregate_separates_recorded_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    powers: tuple[int | None, int | None],
    aggregate: str,
) -> None:
    app, path = fixture_app(tmp_path)
    state = json.loads(path.read_text())
    state["state"] = "failed_cleanup"
    state["cleanup_failures"] = ["preserved failure"]
    state["vms"]["peer"] = {"domain": "labctl-owned-peer", "state": "ready"}
    path.write_text(json.dumps(state))
    libvirt = Libvirt()
    libvirt.states = {
        f"labctl-owned-{name}": power
        for name, power in zip(("node", "peer"), powers, strict=True)
        if power is not None
    }
    monkeypatch.setattr(runtime, "_run_command", libvirt.run)
    result = app.execute(parse_args(command))
    row = result.data if "inspect" in command else result.data[0]
    assert row["state"] == aggregate
    assert row["recorded_state"] == "failed_cleanup"
    assert row["cleanup_failures"] == ["preserved failure"]
    assert row["observation"]["state"] == aggregate
    assert len([call for call in libvirt.calls if call[4] == "domstats"]) == 1
    if "inspect" in command:
        assert row["vms"]["node"]["recorded_state"] == "ready"
    assert json.loads(path.read_text()) == state


def test_vm_inspect_is_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = fixture_app(tmp_path)
    monkeypatch.setattr(runtime, "_run_command", Libvirt().run)
    row = app.execute(parse_args(["vm", "inspect", "LX001", "node"])).data
    assert row["state"] == "stopped"
    assert row["recorded_state"] == "ready"
    assert row["domain"] == "labctl-owned-node"


@pytest.mark.parametrize(
    "command", [["labs"], ["vms"], ["vm", "ls", "--lab", "LX001"], ["lab", "inspect", "LX001"]]
)
def test_status_does_not_migrate_old_state(tmp_path: Path, command: list[str]) -> None:
    app, path = fixture_app(tmp_path)
    path.write_text(json.dumps({"schema_version": 0, "lab_id": "LX001", "uri": "qemu:///system"}))
    before = path.read_bytes()
    app.execute(parse_args(command))
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.v0.bak").exists()


@pytest.mark.parametrize(
    "command",
    [
        ["labs", "--active"],
        ["lab", "inspect", "LX001"],
        ["vms"],
        ["vm", "inspect", "LX001", "node"],
    ],
)
def test_pending_operation_is_separate_and_does_not_recover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
) -> None:
    app, path = fixture_app(tmp_path)
    journal = path.parent.parent / "transactions/LX001.json"
    journal.parent.mkdir()
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "LX001",
                "operation": "reset",
                "phase": "prepared",
                "selected_vms": ["node"],
                "private_key": "SECRET",
            }
        )
    )
    before = journal.read_bytes()
    monkeypatch.setattr(runtime, "_run_command", Libvirt().run)
    result = app.execute(parse_args(command))
    row = result.data if "inspect" in command else result.data[0]
    assert row["state"] == "stopped"
    assert row["pending_operation"]["operation"] == "reset"
    assert row["pending_operation"]["phase"] == "prepared"
    assert row["pending_operation"]["selected_vms"] == ["node"]
    assert "SECRET" not in json.dumps(result.data)
    assert journal.read_bytes() == before
    assert "pending" in " ".join(result.diagnostics).lower()


def test_uncreated_inspect_available_and_journal_only_not_available(tmp_path: Path) -> None:
    app, path = fixture_app(tmp_path)
    path.unlink()
    result = app.execute(parse_args(["lab", "inspect", "LX001"]))
    assert result.data["state"] == "available"
    assert result.data["recorded_state"] is None
    journal = path.parent.parent / "transactions/LX001.json"
    journal.parent.mkdir()
    journal.write_text(json.dumps({"schema_version": 1, "id": "LX001", "operation": "create"}))
    row = app.execute(parse_args(["labs", "--active"])).data[0]
    assert row["state"] == "unknown"
    assert row["recorded_state"] is None
    assert row["pending_operation"]["operation"] == "create"


@pytest.mark.parametrize("command", [["labs", "--available"], ["--provider", "other", "labs"]])
def test_filtered_out_labs_do_not_query_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    app, _ = fixture_app(tmp_path)
    calls = []

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        return Libvirt().run(argv, **kwargs)

    monkeypatch.setattr(runtime, "_run_command", run)
    app.execute(parse_args(command))
    assert not calls


def test_empty_saved_lab_is_unknown_with_diagnostic(tmp_path: Path) -> None:
    app, path = fixture_app(tmp_path)
    state = json.loads(path.read_text())
    state["vms"] = {}
    path.write_text(json.dumps(state))
    row = app.execute(parse_args(["labs", "--active"])).data[0]
    assert row["state"] == "unknown"
    assert row["observation"]["diagnostics"]


@pytest.mark.parametrize("field", ["operation", "phase"])
def test_malformed_pending_summary_is_safe(tmp_path: Path, field: str) -> None:
    app, path = fixture_app(tmp_path)
    path.unlink()
    journal = path.parent.parent / "transactions/LX001.json"
    journal.parent.mkdir()
    journal.write_text(json.dumps({"schema_version": 1, "id": "LX001", field: ["SECRET"]}))
    result = app.execute(parse_args(["labs", "--active"]))
    assert result.data[0]["pending_operation"]["operation"] == "unknown"
    assert "SECRET" not in json.dumps(result.data)
