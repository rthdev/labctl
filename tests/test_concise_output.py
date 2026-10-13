"""Human summaries exercise the CLI boundary with full lifecycle results."""

import json
import shutil
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_runtime_status import Libvirt, fixture_app

from labctl import runtime
from labctl.application import Application
from labctl.cli import main
from labctl.definitions import directory_digest
from labctl.runtime import observe_states


@pytest.mark.parametrize("group", ["lab", "vm"])
@pytest.mark.parametrize("command,verb", [("start", "started"), ("stop", "stopped")])
@pytest.mark.parametrize("flag", [None, "-v", "--verbose", "--debug", "--json", "--quiet"])
@pytest.mark.parametrize("placement", ["before", "after"])
def test_power_modes(tmp_path, monkeypatch, group, command, verb, flag, placement):
    app = Application(data_root=tmp_path, state_root=tmp_path, config_root=tmp_path)
    # Runtime-enriched results must not bypass the concise renderer.
    state = observe_states({"LX001": {"id": "LX001", "state": "ready", "vms": {}}})["LX001"]
    lifecycle = SimpleNamespace(
        start=lambda *a, **kw: state,
        stop=lambda *a, **kw: state,
        stop_vm=lambda *a, **kw: state,
    )
    monkeypatch.setattr(app, "_orchestrator", lambda config: lifecycle)
    argv = [group, command, "LX001"] + (["node"] if group == "vm" else [])
    if flag:
        argv = [flag, *argv] if placement == "before" else [*argv, flag]
    out, err = StringIO(), StringIO()
    assert main(argv, application=app, stdout=out, stderr=err) == 0, err.getvalue()
    if flag == "--quiet":
        assert out.getvalue() == err.getvalue() == ""
    elif flag == "--json":
        assert json.loads(out.getvalue())["data"] == state
    elif flag:
        assert "recorded_state: ready\n" in out.getvalue()
        assert "observation:" in out.getvalue()
    else:
        identity = "Lab LX001" if group == "lab" else "VM LX001/node"
        assert out.getvalue() == f"{identity} {verb}.\n"
    assert "\x1b" not in out.getvalue() + err.getvalue()


def snapshot_app(tmp_path, monkeypatch):
    app, state_path = fixture_app(tmp_path)
    snapshot = app.data_root / "instances/LX001/definition"
    source = Path(__file__).parents[1] / "src/labctl/data/labs/LX001"
    shutil.copytree(source, snapshot)
    definition = yaml.safe_load((snapshot / "lab.yaml").read_text())
    definition.update(
        title="Original title",
        goal="Original goal",
        instructions="## Assignment\n\n1. Keep original work.\n2. Verify it.\n",
    )
    (snapshot / "lab.yaml").write_text(yaml.safe_dump(definition))
    state = json.loads(state_path.read_text())
    state["definition_digest"] = directory_digest(snapshot)
    state_path.write_text(json.dumps(state))
    monkeypatch.setattr(runtime, "_run_command", Libvirt().run)
    return app, state_path, snapshot, definition


@pytest.mark.parametrize("flag", [None, "-v", "--verbose", "--debug", "--json", "--quiet"])
def test_inspect_uses_snapshot_and_live_state(tmp_path, monkeypatch, flag):
    app, state_path, snapshot, definition = snapshot_app(tmp_path, monkeypatch)
    before = state_path.read_bytes(), state_path.stat().st_mtime_ns
    out, err = StringIO(), StringIO()
    argv = ["lab", "inspect", "LX001"] + ([flag] if flag else [])
    assert main(argv, application=app, stdout=out, stderr=err) == 0, err.getvalue()
    if flag == "--quiet":
        assert out.getvalue() == err.getvalue() == ""
    elif flag == "--json":
        data = json.loads(out.getvalue())["data"]
        assert data["instructions"] == definition["instructions"]
        assert data["state"] == "stopped"
        assert data["recorded_state"] == "ready"
        assert data["definition"] == str(snapshot / "lab.yaml")
    elif flag:
        assert "recorded_state: ready\n" in out.getvalue()
        assert definition["instructions"] in out.getvalue()
        assert "title: Original title\n" in out.getvalue()
    else:
        assert out.getvalue() == (
            "id: LX001\nstate: stopped\ntitle: Original title\ngoal: Original goal\n"
            "instructions:\n## Assignment\n\n1. Keep original work.\n2. Verify it.\n"
        )
    assert "\x1b" not in out.getvalue() + err.getvalue()
    assert (state_path.read_bytes(), state_path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("damage", ["missing", "invalid", "altered", "wrong-id"])
@pytest.mark.parametrize("flag", [None, "--json", "--quiet", "-v"])
def test_orphan_inspect_does_not_substitute_current_catalogue(tmp_path, monkeypatch, damage, flag):
    app, state_path, snapshot, _ = snapshot_app(tmp_path, monkeypatch)
    source = snapshot / "lab.yaml"
    if damage == "missing":
        shutil.rmtree(snapshot)
    elif damage == "invalid":
        source.write_text("invalid: [")
    elif damage == "altered":
        source.write_text(source.read_text().replace("Original title", "Altered title"))
    else:
        source.write_text(source.read_text().replace("LX001", "LX002"))
        state = json.loads(state_path.read_text())
        state["definition_digest"] = directory_digest(snapshot)
        state_path.write_text(json.dumps(state))
    out, err = StringIO(), StringIO()
    assert (
        main(
            ["lab", "inspect", "LX001", *([flag] if flag else [])],
            application=app,
            stdout=out,
            stderr=err,
        )
        == 0
    )
    if flag == "--quiet":
        assert out.getvalue() == err.getvalue() == ""
    elif flag == "--json":
        data = json.loads(out.getvalue())["data"]
        assert data["instructions"] is None
        assert data["definition_diagnostic"]
        assert err.getvalue() == ""
    else:
        assert "state: stopped\n" in out.getvalue()
        if flag is None:
            assert "instructions:\nunavailable\n" in out.getvalue()
        assert "assignment unavailable" in err.getvalue()
        assert "Original" not in out.getvalue()
        assert str(snapshot) not in err.getvalue()


def test_available_inspect_has_id_and_canonical_assignment(tmp_path):
    app = Application(data_root=tmp_path, state_root=tmp_path, config_root=tmp_path)
    out, err = StringIO(), StringIO()
    assert main(["lab", "inspect", "LX001"], application=app, stdout=out, stderr=err) == 0
    assert out.getvalue().startswith("id: LX001\nstate: available\ntitle:")
    assert "\ninstructions:\nWork on node" in out.getvalue()
    assert "definition:" not in out.getvalue()
    assert err.getvalue() == ""


@pytest.mark.parametrize("group", ["lab", "vm"])
@pytest.mark.parametrize("command", ["start", "stop"])
@pytest.mark.parametrize("flag", [None, "-v", "--debug", "--json", "--quiet"])
def test_power_failure_never_claims_success(group, command, flag):
    from labctl.application import CommandResult

    app = SimpleNamespace(
        execute=lambda args: CommandResult(group, {"reason": "ownership refused"}, status=6)
    )
    argv = [group, command, "LX001"] + (["node"] if group == "vm" else [])
    out, err = StringIO(), StringIO()
    assert main([*argv, *([flag] if flag else [])], application=app, stdout=out, stderr=err) == 6
    assert "started." not in out.getvalue()
    assert "stopped." not in out.getvalue()
    assert "failed" in err.getvalue()
    if flag == "--json":
        assert json.loads(err.getvalue())["error"]["details"]["reason"] == "ownership refused"
    elif flag == "--quiet":
        assert out.getvalue() == ""


@pytest.mark.parametrize("flag", [None, "-v", "--debug", "--json", "--quiet"])
def test_inspect_keeps_runtime_and_lifecycle_warnings(tmp_path, monkeypatch, flag):
    app, state_path, _, _ = snapshot_app(tmp_path, monkeypatch)
    state = json.loads(state_path.read_text())
    state.update(state="failed_cleanup", cleanup_failures=["retained disk"])
    state["provider_uri"] = ""
    state_path.write_text(json.dumps(state))
    out, err = StringIO(), StringIO()
    argv = ["lab", *([flag] if flag else []), "inspect", "LX001"]
    assert main(argv, application=app, stdout=out, stderr=err) == 0, err.getvalue()
    if flag == "--quiet":
        assert out.getvalue() == err.getvalue() == ""
    elif flag == "--json":
        data = json.loads(out.getvalue())["data"]
        assert data["state"] == "unknown"
        assert data["recorded_state"] == "failed_cleanup"
        assert data["cleanup_failures"] == ["retained disk"]
        assert data["observation"]["diagnostics"]
    else:
        assert "state: unknown\n" in out.getvalue()
        assert "recorded lifecycle is failed_cleanup" in err.getvalue()
        assert "URI" in err.getvalue()
        if flag:
            assert "retained disk" in out.getvalue()
        else:
            assert "retained disk" not in out.getvalue()


@pytest.mark.parametrize("flag", [None, "-v", "--json", "--quiet"])
def test_pending_inspect_is_not_hidden_or_recovered(tmp_path, monkeypatch, flag):
    app, _, _, _ = snapshot_app(tmp_path, monkeypatch)
    journal = app.state_root / "transactions/LX001.json"
    journal.parent.mkdir()
    journal.write_text(
        json.dumps({"schema_version": 1, "id": "LX001", "operation": "reset", "phase": "prepared"})
    )
    before = journal.read_bytes(), journal.stat().st_mtime_ns
    out, err = StringIO(), StringIO()
    assert (
        main(
            ["lab", "inspect", "LX001", *([flag] if flag else [])],
            application=app,
            stdout=out,
            stderr=err,
        )
        == 0
    )
    if flag == "--quiet":
        assert out.getvalue() == err.getvalue() == ""
    elif flag == "--json":
        assert json.loads(out.getvalue())["data"]["pending_operation"]["operation"] == "reset"
    else:
        assert "pending reset transaction" in err.getvalue()
        assert ("pending_operation:" in out.getvalue()) == (flag == "-v")
    assert (journal.read_bytes(), journal.stat().st_mtime_ns) == before


def test_snapshot_inspect_does_not_require_current_catalogue(tmp_path, monkeypatch):
    app, _, _, _ = snapshot_app(tmp_path, monkeypatch)

    def unavailable(config):
        raise AssertionError("existing instances must not discover current definitions")

    monkeypatch.setattr(app, "_definitions", unavailable)

    class Terminal(StringIO):
        def isatty(self):
            return True

    out, err = Terminal(), StringIO()
    assert main(["lab", "inspect", "LX001"], application=app, stdout=out, stderr=err) == 0
    assert "title: Original title" in out.getvalue()
    assert "\x1b" not in out.getvalue()
    assert err.getvalue() == ""
