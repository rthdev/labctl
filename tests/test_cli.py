from __future__ import annotations

import json
import logging
import time
from argparse import Namespace
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

import labctl.cli as cli_module
from labctl.application import Application, CommandResult
from labctl.cli import main, parse_args
from labctl.errors import ExitStatus, LabctlError


class FakeApplication:
    def __init__(self, result: CommandResult | None = None) -> None:
        self.result = result or CommandResult("labs", [], ("ID", "STATE"))
        self.calls: list[Namespace] = []

    def execute(self, args: Namespace) -> CommandResult:
        self.calls.append(args)
        return self.result


class TtyBuffer(StringIO):
    def isatty(self) -> bool:
        return True


def invoke(
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    application: Any | None = None,
) -> tuple[int, str, str]:
    status = main(argv, application=application)
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def assert_published_json_output(document: dict[str, Any]) -> None:
    schema_path = Path(__file__).parents[1] / "docs/schemas/json-output-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(document)


def test_config_error_maps_to_usage_exit_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir()
    (config_root / "config.toml").write_text("unknown = true\n", encoding="utf-8")

    status, stdout, stderr = invoke(
        capsys,
        ["lab", "ls"],
        Application(
            data_root=tmp_path / "data",
            state_root=tmp_path / "state",
            config_root=config_root,
        ),
    )

    assert status == ExitStatus.USAGE
    assert stdout == ""
    assert "unknown configuration setting: unknown" in stderr


@pytest.mark.parametrize(
    ("group", "commands"),
    [
        (
            "lab",
            (
                "ls",
                "list",
                "create",
                "inspect",
                "start",
                "stop",
                "restart",
                "grade",
                "reset",
                "reconcile",
                "rm",
                "ssh",
                "console",
            ),
        ),
        ("vm", ("ls", "list", "inspect", "start", "stop", "restart", "ssh", "console", "rm")),
        ("image", ("pull", "import", "ls", "list", "inspect", "rm")),
        ("provider", ("ls", "list", "inspect", "doctor")),
    ],
)
def test_complete_command_surface(group: str, commands: tuple[str, ...]) -> None:
    for command in commands:
        with pytest.raises(SystemExit) as caught:
            parse_args([group, command, "--help"])
        assert caught.value.code == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "--provider", "kvm", "lab", "ls"],
        ["lab", "--json", "--provider", "kvm", "ls"],
        ["lab", "ls", "--json", "--provider", "kvm"],
        ["vm", "inspect", "demo", "node", "--uri", "qemu:///session", "-vv"],
        ["provider", "doctor", "--allow-definition-override", "--debug"],
    ],
)
def test_global_options_are_accepted_at_every_level(argv: list[str]) -> None:
    args = parse_args(argv)
    if "--json" in argv:
        assert args.json is True
        assert args.provider == "kvm"
    if "-vv" in argv:
        assert args.verbose == 2
        assert args.uri == "qemu:///session"
    if "--debug" in argv:
        assert args.debug is True
        assert args.allow_definition_override is True


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "lab", "ls", "--quiet"],
        ["-v", "provider", "--debug", "ls"],
        ["lab", "ssh", "demo", "--json"],
        ["--json", "vm", "console", "demo/node"],
        ["vm", "rm", "demo", "node"],
        ["lab", "ls", "--available", "--active"],
        ["image", "import", "local:test", "disk.qcow2", "--checksum", "bad"],
    ],
)
def test_invalid_combinations_are_rejected_before_dispatch(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    application = FakeApplication()
    status, _stdout, stderr = invoke(capsys, argv, application)
    assert status == ExitStatus.USAGE
    assert stderr
    assert application.calls == []


def test_json_success_and_error_envelopes_are_stable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    success = FakeApplication(CommandResult("images", [{"id": "rocky:9", "cached": False}]))
    status, stdout, stderr = invoke(capsys, ["image", "ls", "--json"], success)
    assert status == 0
    assert stderr == ""
    success_document = json.loads(stdout)
    assert success_document == {
        "schema_version": 1,
        "ok": True,
        "kind": "images",
        "data": [{"cached": False, "id": "rocky:9"}],
    }
    assert_published_json_output(success_document)

    class BrokenApplication:
        def execute(self, _args: Namespace) -> CommandResult:
            raise LabctlError("install libvirt", ExitStatus.PREREQUISITE)

    status, stdout, stderr = invoke(capsys, ["provider", "doctor", "--json"], BrokenApplication())
    assert status == ExitStatus.PREREQUISITE
    assert stdout == ""
    error_document = json.loads(stderr)
    assert error_document == {
        "schema_version": 1,
        "ok": False,
        "error": {"code": "prerequisite", "message": "install libvirt"},
    }
    assert_published_json_output(error_document)

    failed_result = FakeApplication(
        CommandResult("doctor", [{"name": "cloud-localds", "status": "fail"}], status=5)
    )
    status, stdout, stderr = invoke(capsys, ["provider", "doctor", "--json"], failed_result)
    assert status == ExitStatus.PREREQUISITE
    assert stdout == ""
    error = json.loads(stderr)["error"]
    assert {key: error[key] for key in ("code", "message")} == {
        "code": "prerequisite",
        "message": "mandatory provider checks failed",
    }
    assert error["details"] == [{"name": "cloud-localds", "status": "fail"}]


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "--help"],
        ["lab", "--json", "--help"],
        ["lab", "create", "--json", "--help"],
    ],
)
def test_json_help_uses_injected_stdout_success_envelope(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    application = FakeApplication()

    status, stdout, stderr = invoke(capsys, argv, application)

    assert status == 0
    assert stderr == ""
    document = json.loads(stdout)
    assert document["schema_version"] == 1
    assert document["ok"] is True
    assert document["kind"] == "help"
    assert document["data"]["text"].startswith("usage: labctl")
    assert_published_json_output(document)
    assert application.calls == []


def test_human_table_quiet_and_alias_dispatch(capsys: pytest.CaptureFixture[str]) -> None:
    result = CommandResult(
        "labs",
        [{"id": "LX001", "state": "available"}],
        ("id", "state"),
    )
    application = FakeApplication(result)
    status, stdout, stderr = invoke(capsys, ["lab", "list"], application)
    assert (status, stderr) == (0, "")
    assert stdout.splitlines() == ["ID     STATE", "LX001  available"]
    assert application.calls[0].command == "ls"

    status, stdout, stderr = invoke(capsys, ["lab", "ls", "--quiet"], application)
    assert (status, stdout, stderr) == (0, "", "")


def test_unexpected_errors_do_not_emit_tracebacks(capsys: pytest.CaptureFixture[str]) -> None:
    class BrokenApplication:
        def execute(self, _args: Namespace) -> CommandResult:
            raise RuntimeError("backend exploded")

    status, stdout, stderr = invoke(capsys, ["lab", "ls"], BrokenApplication())
    assert status == ExitStatus.OPERATION
    assert stdout == ""
    assert stderr == "error: backend exploded\n"
    assert "Traceback" not in stderr


def test_application_lists_catalog_and_state(tmp_path: Path) -> None:
    state = tmp_path / "state" / "labs"
    state.mkdir(parents=True)
    (state / "demo.json").write_text(
        '{"schema_version":1,"id":"demo","provider_uri":"qemu:///session","state":"running"}\n'
    )
    app = Application(data_root=tmp_path / "data", state_root=tmp_path / "state")

    images = app.execute(parse_args(["image", "ls"]))
    labs = app.execute(parse_args(["lab", "ls", "--active"]))

    assert {item["id"] for item in images.data} == {"rocky:9", "rocky:10", "fedora:44"}
    assert labs.data == [
        {
            "id": "demo",
            "title": "",
            "provider": "kvm",
            "state": "running",
            "uri": "qemu:///session",
        }
    ]


def test_application_discovers_bundled_lab_definitions(tmp_path: Path) -> None:
    app = Application(data_root=tmp_path / "data", state_root=tmp_path / "state")

    result = app.execute(parse_args(["lab", "ls"]))

    assert {row["id"] for row in result.data} == {
        "AN001",
        "AN002",
        "AN101",
        "AN102",
        "AN201",
        "AN202",
        "AN301",
        "AN302",
        "AN401",
        "AN402",
        "CT001",
        "CT002",
        "CT101",
        "CT102",
        "CT201",
        "CT202",
        "CT301",
        "CT302",
        "CT401",
        "CT402",
        "LX001",
        "LX002",
        "LX101",
        "LX102",
        "LX201",
        "LX202",
        "LX301",
        "LX302",
        "LX401",
        "LX402",
    }
    assert all(row["state"] == "available" for row in result.data)


def test_application_lists_imported_images_not_in_catalog(tmp_path: Path) -> None:
    references = tmp_path / "data" / "images" / "refs"
    references.mkdir(parents=True)
    (references / "local%3Atest.json").write_text(
        json.dumps({"id": "local:test", "sha256": "a" * 64}), encoding="utf-8"
    )
    app = Application(data_root=tmp_path / "data", state_root=tmp_path / "state")

    result = app.execute(parse_args(["image", "ls", "--cached"]))

    assert result.data == [
        {
            "id": "local:test",
            "format": "qcow2",
            "available": False,
            "cached": True,
            "sha256": "a" * 64,
            "reason": "locally imported image",
        }
    ]


def test_ssh_console_preserve_child_status(tmp_path: Path) -> None:
    state = tmp_path / "state" / "labs"
    state.mkdir(parents=True)
    (state / "demo.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "demo",
                "provider_uri": "qemu:///session",
                "vms": {
                    "node": {
                        "address": "192.0.2.4",
                        "ssh_user": "student",
                        "identity_file": "/keys/id",
                        "known_hosts": "/keys/known_hosts",
                        "domain": "labctl-demo-node",
                    }
                },
            }
        )
    )
    calls: list[list[str]] = []

    def child(argv: list[str]) -> int:
        calls.append(argv)
        return 23

    app = Application(data_root=tmp_path / "data", state_root=tmp_path / "state", child=child)

    assert app.execute(parse_args(["vm", "ssh", "demo", "node"])).status == 23
    assert calls[0] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=/keys/known_hosts",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        "/keys/id",
        "--",
        "student@192.0.2.4",
    ]
    assert app.execute(parse_args(["lab", "console", "demo", "node"])).status == 23
    assert calls[1] == [
        "virsh",
        "--connect",
        "qemu:///session",
        "console",
        "labctl-demo-node",
    ]


def test_json_grading_failure_emits_one_structured_document_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    application = FakeApplication(
        CommandResult(
            "grade",
            {"outcome": "fail", "stderr": ["raw diagnostic"]},
            status=ExitStatus.GRADING,
            diagnostics=("raw diagnostic",),
        )
    )

    status, stdout, stderr = invoke(capsys, ["lab", "grade", "LX001", "--json"], application)

    assert status == ExitStatus.GRADING
    assert stdout == ""
    document = json.loads(stderr)
    assert document["error"]["details"]["stderr"] == ["raw diagnostic"]
    assert stderr.count("\n") == 1


def test_human_grading_colours_each_pass_and_fail_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    application = FakeApplication(
        CommandResult(
            "grade",
            {
                "outcome": "fail",
                "stdout": [
                    "PASS LX001: group exists",
                    "FAIL LX001: permissions are wrong",
                ],
            },
            status=ExitStatus.GRADING,
        )
    )
    output = TtyBuffer()
    errors = StringIO()

    status = main(
        ["lab", "grade", "LX001"],
        application=application,
        stdout=output,
        stderr=errors,
    )

    assert status == ExitStatus.GRADING
    assert output.getvalue().splitlines() == [
        "\033[32mPASS\033[0m LX001: group exists",
        "\033[31mFAIL\033[0m LX001: permissions are wrong",
    ]


def test_lab_create_shows_moving_spinner_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowApplication(FakeApplication):
        def execute(self, args: Namespace) -> CommandResult:
            time.sleep(0.03)
            return CommandResult("lab", {"id": args.id, "state": "ready"})

    monkeypatch.setattr(cli_module, "_SPINNER_INTERVAL", 0.001)
    output = TtyBuffer()

    status = main(
        ["lab", "create", "LX001"],
        application=SlowApplication(),
        stdout=output,
        stderr=StringIO(),
    )

    rendered = output.getvalue()
    assert status == ExitStatus.SUCCESS
    assert "Creating lab LX001" in rendered
    assert sum(frame in rendered for frame in "|/-\\") >= 2
    assert rendered.endswith("Lab LX001 created.\n")


def test_lab_create_default_output_is_only_a_creation_confirmation() -> None:
    output = StringIO()
    errors = StringIO()
    application = FakeApplication(
        CommandResult(
            "lab",
            {
                "id": "LX001",
                "state": "ready",
                "provider_uri": "qemu:///system",
                "network": "labctl-network",
            },
        )
    )

    status = main(
        ["lab", "create", "LX001"],
        application=application,
        stdout=output,
        stderr=errors,
    )

    assert status == ExitStatus.SUCCESS
    assert output.getvalue() == "Lab LX001 created.\n"
    assert errors.getvalue() == ""


def test_lab_create_verbose_output_retains_the_full_result() -> None:
    output = StringIO()
    errors = StringIO()
    application = FakeApplication(
        CommandResult("lab", {"id": "LX001", "state": "ready", "network": "labctl-network"})
    )

    status = main(
        ["lab", "create", "LX001", "-v"],
        application=application,
        stdout=output,
        stderr=errors,
    )

    assert status == ExitStatus.SUCCESS
    assert output.getvalue().splitlines() == [
        "id: LX001",
        "state: ready",
        "network: labctl-network",
    ]
    assert errors.getvalue() == ""


def test_lab_create_debug_emits_execution_steps_and_full_result() -> None:
    class DebugApplication(FakeApplication):
        def execute(self, args: Namespace) -> CommandResult:
            logging.getLogger("labctl.test").debug("executing create step for %s", args.id)
            return CommandResult("lab", {"id": args.id, "state": "ready"})

    output = StringIO()
    errors = StringIO()

    status = main(
        ["lab", "create", "LX001", "--debug"],
        application=DebugApplication(),
        stdout=output,
        stderr=errors,
    )

    assert status == ExitStatus.SUCCESS
    assert output.getvalue().splitlines() == ["id: LX001", "state: ready"]
    assert errors.getvalue() == "DEBUG labctl.test: executing create step for LX001\n"


@pytest.mark.parametrize(
    ("mode", "expected_stdout"),
    [
        (
            "--json",
            '{"data": {"id": "LX001", "state": "ready"}, "kind": "lab", '
            '"ok": true, "schema_version": 1}\n',
        ),
        ("--quiet", ""),
    ],
)
def test_lab_create_machine_modes_suppress_debug_traces(
    mode: str,
    expected_stdout: str,
) -> None:
    class DebugApplication(FakeApplication):
        def execute(self, args: Namespace) -> CommandResult:
            logging.getLogger("labctl.test").debug("hidden create step for %s", args.id)
            return CommandResult("lab", {"id": args.id, "state": "ready"})

    output = StringIO()
    errors = StringIO()

    status = main(
        ["lab", "create", "LX001", "--debug", mode],
        application=DebugApplication(),
        stdout=output,
        stderr=errors,
    )

    assert status == ExitStatus.SUCCESS
    assert output.getvalue() == expected_stdout
    assert errors.getvalue() == ""
