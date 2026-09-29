from contextlib import contextmanager
from io import StringIO
from types import SimpleNamespace

import pytest

from labctl.application import Application
from labctl.cli import main, parse_args
from labctl.definitions import GradingDefinition
from labctl.grading import GradeOutcome, GradeResult


@pytest.fixture
def grading_app(tmp_path, monkeypatch):
    events = []
    snapshot = tmp_path / "data/instances/AN001/definition/lab.yaml"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("fixture")
    state = {
        "provider_uri": "qemu:///system",
        "vms": {
            name: {
                "hostname": name,
                "address": "192.0.2.1",
                "ssh_user": "student",
                "identity_file": str(tmp_path / "key"),
                "known_hosts": str(tmp_path / "known_hosts"),
                "state": "ready",
            }
            for name in ("controller", "target")
        },
    }

    class Session:
        @contextmanager
        def grade_session(self, lab):
            events.append("lock")
            try:
                yield self
            finally:
                events.append("unlock")

        def reconcile(self, *, repair):
            assert not repair
            events.append("validate")
            return []

        def reset(self, images, *, vm_names):
            assert vm_names == ("target",)
            events.append("reset")
            return state

        def start(self):
            events.append("start")
            return state

    def confirm(prompt):
        events.append(prompt)
        return False

    app = Application(data_root=tmp_path / "data", state_root=tmp_path / "state", confirm=confirm)
    monkeypatch.setattr(app, "_orchestrator", lambda config: Session())
    monkeypatch.setattr(app, "_state", lambda lab: state)
    monkeypatch.setattr(
        "labctl.application.load_definition",
        lambda path: SimpleNamespace(
            title="Test",
            goal="Test",
            grader=tmp_path / "grade",
            grading=GradingDefinition(("target",)),
        ),
    )

    def grader(*args, **kwargs):
        events.append("grade")
        return GradeResult(GradeOutcome.PASS, 0, ("PASS example",), ())

    monkeypatch.setattr("labctl.application.run_grader", grader)
    return app, events, state


@pytest.mark.parametrize("flags", [[], ["--reset"], ["--reset", "--yes"], ["--yes"]])
def test_grade_alias_shares_options(flags):
    assert vars(parse_args(["grade", "AN001", *flags])) == vars(
        parse_args(["lab", "grade", "AN001", *flags])
    )


@pytest.mark.parametrize("flags", [[], ["--json"], ["--quiet"]])
def test_reset_requires_yes_without_interactive_human_input(grading_app, monkeypatch, flags):
    app, events, _ = grading_app
    monkeypatch.setattr("sys.stdin", StringIO())
    out, err = StringIO(), StringIO()
    assert (
        main(["lab", "grade", "AN001", "--reset", *flags], application=app, stdout=out, stderr=err)
        == 2
    )
    assert "--yes" in err.getvalue()
    assert events == ["lock", "validate", "unlock"]


class TTY(StringIO):
    def isatty(self):
        return True


def test_reset_decline_names_scope_before_mutation(grading_app, monkeypatch):
    app, events, _ = grading_app
    monkeypatch.setattr("sys.stdin", TTY())
    out, err = TTY(), StringIO()
    assert main(["lab", "grade", "AN001", "--reset"], application=app, stdout=out, stderr=err) == 2
    assert len(events) == 4
    assert events[:2] == ["lock", "validate"]
    assert "AN001/target" in events[2]
    assert "disk" in events[2] and "destroy" in events[2]
    assert "controller" in events[2] and "preserv" in events[2]
    assert events[-1] == "unlock"
    assert "\r" not in out.getvalue()


def test_yes_only_bypasses_consent_not_selectivity(grading_app):
    app, events, _ = grading_app
    out = StringIO()
    assert (
        main(
            ["lab", "grade", "AN001", "--reset", "--yes"],
            application=app,
            stdout=out,
            stderr=StringIO(),
        )
        == 0
    )
    assert events == ["lock", "validate", "reset", "start", "grade", "unlock"]
    assert "clean-baseline" in out.getvalue()


def test_progress_stage_change_clears_previous_longer_label(monkeypatch):
    import time

    from labctl.cli import _grading_progress

    monkeypatch.setattr("labctl.cli._SPINNER_INTERVAL", 0.005)
    out = TTY()
    with _grading_progress(out, "Grading lab AN001", enabled=True) as update:
        update("Resetting declared targets")
        time.sleep(0.025)
        update("Running grader")
        time.sleep(0.025)
    frames = out.getvalue().split("\r")[1:]
    assert any("Resetting" in frame for frame in frames)
    assert any("Running grader" in frame for frame in frames)
    assert all(frame.startswith("\033[2K") for frame in frames)


def test_current_state_reports_mode_and_reset_hint(grading_app):
    app, events, _ = grading_app
    out = StringIO()
    assert (
        main(["lab", "grade", "AN001", "--yes"], application=app, stdout=out, stderr=StringIO())
        == 0
    )
    assert events == ["lock", "validate", "grade", "unlock"]
    assert "current-state" in out.getvalue()
    assert "labctl lab grade AN001 --reset" in out.getvalue()


@pytest.mark.parametrize("reset", [False, True])
@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_grade_spinner_elapsed_and_cleanup(grading_app, monkeypatch, reset, failure):
    import time

    app, events, _ = grading_app
    monkeypatch.setattr("labctl.cli._SPINNER_INTERVAL", 0.005)
    monkeypatch.setenv("NO_COLOR", "1")
    out = TTY()

    def grade(*args, **kwargs):
        time.sleep(0.035)
        if failure:
            raise failure("interrupted")
        return GradeResult(GradeOutcome.PASS, 0, ("PASS example",), ())

    monkeypatch.setattr("labctl.application.run_grader", grade)
    args = ["grade", "AN001", *(["--reset", "--yes"] if reset else [])]
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            main(args, application=app, stdout=out, stderr=StringIO())
    else:
        assert main(args, application=app, stdout=out, stderr=StringIO()) == (6 if failure else 0)
    text = out.getvalue()
    assert text.count("Grading lab AN001") >= 2
    assert "elapsed" in text and "Running grader" in text
    assert ("clean-baseline" if reset else "current-state") in text
    assert "\r\033[2K" in text
    if not failure:
        assert text.index("\r\033[2K", text.rfind("elapsed")) < text.index("PASS example")
    size = len(text)
    time.sleep(0.02)
    assert len(out.getvalue()) == size
    assert events[-1] == "unlock"


@pytest.mark.parametrize("flags", [["--json"], ["--quiet"], ["--debug"], []])
@pytest.mark.parametrize("reset", [False, True])
def test_grade_no_spinner_in_machine_debug_or_redirected_output(grading_app, flags, reset):
    app, _, _ = grading_app
    out = TTY() if flags else StringIO()
    assert (
        main(
            ["grade", "AN001", *flags, *(["--reset", "--yes"] if reset else [])],
            application=app,
            stdout=out,
            stderr=StringIO(),
        )
        == 0
    )
    assert "\r" not in out.getvalue() and "\033[2K" not in out.getvalue()
    if flags == ["--quiet"]:
        assert not out.getvalue()


def test_current_state_does_not_start_stopped_guests(grading_app):
    app, events, state = grading_app
    state["vms"]["target"]["state"] = "stopped"
    out, err = StringIO(), StringIO()
    assert main(["grade", "AN001"], application=app, stdout=out, stderr=err) == 5
    assert "labctl lab start AN001" in err.getvalue()
    assert events == ["lock", "validate", "unlock"]


@pytest.mark.parametrize("reply", ["", "n", "yes", "Y"])
def test_real_confirmation_default_no_and_acceptance(grading_app, monkeypatch, reply):
    app, events, _ = grading_app
    monkeypatch.setattr("sys.stdin", TTY())
    out = TTY()

    def answer(prompt):
        assert prompt.endswith("[y/N] ")
        assert "AN001/target" in prompt and "destroy" in prompt
        assert events == ["lock", "validate"]
        assert not out.getvalue()
        return reply

    monkeypatch.setattr("builtins.input", answer)
    app.confirm = app._terminal_confirm
    status = main(["grade", "AN001", "--reset"], application=app, stdout=out, stderr=StringIO())
    assert status == (0 if reply in {"yes", "Y"} else 2)
    assert ("reset" in events) == (status == 0)


def test_confirmation_eof_is_declined(grading_app, monkeypatch):
    app, events, _ = grading_app
    monkeypatch.setattr("sys.stdin", TTY())

    def eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    app.confirm = app._terminal_confirm
    assert (
        main(["grade", "AN001", "--reset"], application=app, stdout=TTY(), stderr=StringIO()) == 2
    )
    assert events == ["lock", "validate", "unlock"]


@pytest.mark.parametrize("reset", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_json_grade_metadata_and_single_envelope(grading_app, monkeypatch, reset, failed):
    import json

    app, _, _ = grading_app
    if failed:
        monkeypatch.setattr(
            "labctl.application.run_grader",
            lambda *a, **kw: GradeResult(
                GradeOutcome.FAIL, 1, ("PASS first", "FAIL second"), ("diagnostic",)
            ),
        )
    out, err = TTY(), TTY()
    result = main(
        ["grade", "AN001", "--json", *(["--reset", "--yes"] if reset else [])],
        application=app,
        stdout=out,
        stderr=err,
    )
    assert result == (7 if failed else 0)
    document = json.loads(err.getvalue() if failed else out.getvalue())
    data = document["error"]["details"] if failed else document["data"]
    assert not (out.getvalue() if failed else err.getvalue())
    assert data["mode"] == ("clean-baseline" if reset else "current-state")
    assert data["reset_vms"] == (["target"] if reset else [])
    assert data["reset_available"] and data["lab_id"] == "AN001"
    if failed:
        assert data["stdout"] == ["PASS first", "FAIL second"]
        assert data["stderr"] == ["diagnostic"]
