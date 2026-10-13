"""Provider boundary, latency and console contracts for runtime observation."""

from __future__ import annotations

import copy
import json
import subprocess
from io import StringIO
from pathlib import Path

import pytest
from test_runtime_status import Libvirt, fixture_app

from labctl import runtime
from labctl.cli import main, parse_args


def test_batches_by_recorded_uri_not_cli_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, path = fixture_app(tmp_path)
    state = json.loads(path.read_text())
    for lab_id, uri in [("LX002", "qemu:///system"), ("LX101", "qemu:///session")]:
        other = copy.deepcopy(state)
        other.update(id=lab_id, provider_uri=uri)
        path.with_name(f"{lab_id}.json").write_text(json.dumps(other))
    libvirt = Libvirt()
    monkeypatch.setattr(runtime, "_run_command", libvirt.run)
    out, err = StringIO(), StringIO()
    assert (
        main(["--uri", "test:///default", "vms", "--json"], application=app, stdout=out, stderr=err)
        == 0
    )
    assert err.getvalue() == ""
    assert {row["state"] for row in json.loads(out.getvalue())["data"]} == {"stopped"}
    assert [call[3] for call in libvirt.calls if call[4] == "domstats"] == [
        "qemu:///system",
        "qemu:///session",
    ]


def test_failed_uri_queried_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, path = fixture_app(tmp_path)
    state = json.loads(path.read_text())
    state["vms"]["peer"] = {"domain": "labctl-owned-peer", "state": "ready"}
    path.write_text(json.dumps(state))
    calls = []

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "permission denied SECRET")

    monkeypatch.setattr(runtime, "_run_command", run)
    out, err = StringIO(), StringIO()
    assert main(["vms"], application=app, stdout=out, stderr=err) == 0
    assert len(calls) == 1
    assert "unknown" in out.getvalue() and "ready" not in out.getvalue()
    assert "authorization" in err.getvalue() and "SECRET" not in err.getvalue()


def test_observation_has_one_total_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, path = fixture_app(tmp_path)
    state = json.loads(path.read_text())
    state["vms"].update(
        {f"peer{i}": {"domain": f"labctl-owned-peer{i}", "state": "ready"} for i in range(5)}
    )
    path.write_text(json.dumps(state))
    clock = [0.0]
    libvirt = Libvirt()
    libvirt.states = {vm["domain"]: 5 for vm in state["vms"].values()}
    timeouts = []

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]
        timeouts.append(kwargs["timeout"])
        clock[0] += kwargs["timeout"]
        return libvirt.run(argv, **kwargs)

    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime, "_run_command", run)
    result = app.execute(parse_args(["vms"]))
    assert timeouts == [3, 3, 3, 1]
    assert clock[0] == 10
    assert result.data[-1]["state"] == "unknown"
    assert "budget" in " ".join(result.data[-1]["observation"]["diagnostics"])


@pytest.mark.parametrize("command", [["vms"], ["vm", "ls"], ["vm", "list"]])
@pytest.mark.parametrize("mode", [[], ["--json"], ["--quiet"], ["--noheading"], ["--debug"]])
def test_cli_aliases_live_without_output_noise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str], mode: list[str]
) -> None:
    app, _ = fixture_app(tmp_path)
    monkeypatch.setattr(runtime, "_run_command", Libvirt().run)
    out, err = StringIO(), StringIO()
    assert main([*command, *mode], application=app, stdout=out, stderr=err) == 0
    assert err.getvalue() == ""
    if "--json" in mode:
        data = json.loads(out.getvalue())
        assert data["schema_version"] == 1 and data["kind"] == "vms"
        assert data["data"][0]["recorded_state"] == "ready"
        assert data["data"][0]["observation"]["state"] == "stopped"
    elif "--quiet" in mode:
        assert out.getvalue() == ""
    else:
        assert (
            out.getvalue()
            == ("" if "--noheading" in mode else "LAB    NAME  STATE\n") + "LX001  node  stopped\n"
        )


@pytest.mark.parametrize("domain", ["--help", "foreign", "bad\nname"])
def test_invalid_recorded_domain_not_queried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, domain: str
) -> None:
    app, path = fixture_app(tmp_path)
    state = json.loads(path.read_text())
    state["vms"]["node"]["domain"] = domain
    path.write_text(json.dumps(state))
    calls = []

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        return Libvirt().run(argv, **kwargs)

    monkeypatch.setattr(runtime, "_run_command", run)
    out, err = StringIO(), StringIO()
    main(["vms"], application=app, stdout=out, stderr=err)
    assert not calls
    assert "unknown" in out.getvalue()


def test_real_timeout_kills_query_helpers_not_just_virsh(tmp_path: Path) -> None:
    import sys
    import time

    marker = tmp_path / "orphan"
    helper = f"import time,pathlib; time.sleep(0.6); pathlib.Path({str(marker)!r}).touch()"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        runtime._run_command(
            [
                sys.executable,
                "-c",
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {helper!r}]); "
                "time.sleep(2)",
            ],
            timeout=0.15,
        )
    assert time.monotonic() - started < 1.5
    time.sleep(0.8)
    assert not marker.exists(), "query helper survived the timeout"


def test_vm_inspect_does_not_probe_peers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, path = fixture_app(tmp_path)
    state = json.loads(path.read_text())
    state["state"] = "failed_cleanup"
    state["cleanup_failures"] = ["saved failure"]
    state["vms"]["peer"] = {"domain": "labctl-owned-peer", "state": "ready"}
    path.write_text(json.dumps(state))
    libvirt = Libvirt()
    libvirt.states["labctl-owned-peer"] = 1
    monkeypatch.setattr(runtime, "_run_command", libvirt.run)
    out, err = StringIO(), StringIO()
    assert (
        main(["vm", "inspect", "LX001", "node", "--json"], application=app, stdout=out, stderr=err)
        == 0
    )
    row = json.loads(out.getvalue())["data"]
    assert [call[5] for call in libvirt.calls if call[4] == "dumpxml"] == ["labctl-owned-node"]
    assert row["recorded_lab_state"] == "failed_cleanup"
    assert row["cleanup_failures"] == ["saved failure"]


def test_vm_list_preserves_lab_failure_in_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, path = fixture_app(tmp_path)
    state = json.loads(path.read_text())
    state["state"] = "failed_cleanup"
    state["cleanup_failures"] = ["saved failure"]
    path.write_text(json.dumps(state))
    monkeypatch.setattr(runtime, "_run_command", Libvirt().run)
    out, err = StringIO(), StringIO()
    assert main(["vms", "--json"], application=app, stdout=out, stderr=err) == 0
    row = json.loads(out.getvalue())["data"][0]
    assert row["recorded_state"] == "ready"
    assert row["recorded_lab_state"] == "failed_cleanup"
    assert row["cleanup_failures"] == ["saved failure"]


def test_legacy_orphan_provider_filter_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, path = fixture_app(tmp_path)
    state = json.loads(path.read_text())
    state.pop("provider")
    state["id"] = "orphan"
    path.unlink()
    path.with_name("orphan.json").write_text(json.dumps(state))
    monkeypatch.setattr(runtime, "_run_command", lambda *a, **kw: pytest.fail("KVM fallback"))
    result = app.execute(parse_args(["--provider", "external", "labs", "--active"]))
    assert result.data[0]["state"] == "unknown"


def test_invalid_provider_text_does_not_escape_diagnostic_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = fixture_app(tmp_path)

    def run(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise UnicodeDecodeError("utf-8", b"SECRET\xff", 6, 7, "invalid")

    monkeypatch.setattr(runtime, "_run_command", run)
    result = app.execute(parse_args(["vms"]))
    assert result.data[0]["state"] == "unknown"
    assert "SECRET" not in str(result)
