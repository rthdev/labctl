from argparse import Namespace
from io import StringIO

import pytest

from labctl.application import CommandResult
from labctl.cli import build_parser, main, parse_args
from labctl.completion import bash_completion
from labctl.errors import ExitStatus

LIST_COMMANDS = [
    argv
    for group in ("lab", "vm", "image", "provider")
    for argv in ([group, "ls"], [group, "list"], [group + "s"])
]


class TableApplication:
    def __init__(self, *, empty: bool = False) -> None:
        self.calls: list[Namespace] = []
        self.result = CommandResult(
            "examples",
            [] if empty else [{"id": "x", "state": "up"}, {"id": "long-id", "state": "down"}],
            ("id", "state"),
            diagnostics=("diagnostic",),
        )

    def execute(self, args: Namespace) -> CommandResult:
        self.calls.append(args)
        return self.result


def invoke(argv: list[str], app: TableApplication) -> tuple[int, str, str]:
    out, err = StringIO(), StringIO()
    status = main(argv, application=app, stdout=out, stderr=err)
    return status, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("argv", LIST_COMMANDS)
def test_noheading_removes_only_header(argv: list[str]) -> None:
    app = TableApplication()
    assert not parse_args(argv).noheading
    assert parse_args([*argv, "--noheading"]).noheading
    normal = invoke(argv, app)
    headerless = invoke([*argv, "--noheading"], app)
    assert normal == (0, "ID       STATE\nx        up\nlong-id  down\n", "diagnostic\n")
    assert headerless == (0, "x        up\nlong-id  down\n", "diagnostic\n")
    assert app.calls[-1].command == "ls"


@pytest.mark.parametrize("argv", LIST_COMMANDS)
@pytest.mark.parametrize(
    ("mode", "empty"),
    [
        ([], True),
        (["--json"], False),
        (["--json"], True),
        (["--quiet"], False),
        (["--quiet"], True),
    ],
)
def test_noheading_preserves_other_output_modes(
    argv: list[str], mode: list[str], empty: bool
) -> None:
    app = TableApplication(empty=empty)
    assert invoke([*argv, *mode, "--noheading"], app) == invoke([*argv, *mode], app)


@pytest.mark.parametrize("argv", LIST_COMMANDS)
def test_noheading_in_list_help_and_completion(argv: list[str]) -> None:
    import json

    out = StringIO()
    assert main([*argv, "--help", "--json"], stdout=out, stderr=StringIO()) == 0
    assert "--noheading" in json.loads(out.getvalue())["data"]["text"]
    state = "root/" + "/".join(argv)
    line = next(
        line for line in bash_completion(build_parser()).splitlines() if f"{state}) words=" in line
    )
    assert "--noheading" in line


@pytest.mark.parametrize("argv", [["lab", "inspect", "demo"], ["provider", "doctor"], ["version"]])
def test_noheading_rejected_for_non_list_commands(argv: list[str]) -> None:
    app = TableApplication()
    status, out, err = invoke([*argv, "--noheading"], app)
    assert status == ExitStatus.USAGE
    assert out == ""
    assert "unrecognized arguments: --noheading" in err
    assert not app.calls
