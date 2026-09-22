"""Command-line parsing, validation, rendering, and process exit handling."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Never, Protocol, TextIO

from labctl import __version__
from labctl.application import Application, CommandResult
from labctl.completion import bash_completion
from labctl.config import ConfigError
from labctl.errors import ExitStatus, LabctlError
from labctl.images import DownloadProgress

_SPINNER_FRAMES = "|/-\\"
_SPINNER_INTERVAL = 0.1


@contextmanager
def _spinner(stream: TextIO, label: str, *, enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return
    stopped = threading.Event()

    def animate() -> None:
        index = 0
        while not stopped.is_set():
            stream.write(f"\r{_SPINNER_FRAMES[index % len(_SPINNER_FRAMES)]} {label}")
            stream.flush()
            index += 1
            stopped.wait(_SPINNER_INTERVAL)

    worker = threading.Thread(target=animate, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()
        stream.write("\r\033[2K")
        stream.flush()


@contextmanager
def _download_progress(
    stream: TextIO, label: str, *, enabled: bool
) -> Iterator[DownloadProgress | None]:
    if not enabled:
        yield None
        return

    def update(downloaded: int, total: int | None) -> None:
        if total is None:
            detail = f"{downloaded} bytes (total unknown)"
        else:
            filled = min(20, downloaded * 20 // total)
            bar = "=" * filled + " " * (20 - filled)
            detail = f"[{bar}] {downloaded * 100 // total}% {downloaded} bytes / {total} bytes"
        stream.write(f"\r\033[2K{label}: {detail}")
        stream.flush()

    try:
        update(0, None)
        yield update
    finally:
        stream.write("\r\033[2K")
        stream.flush()


@contextmanager
def _debug_logging(stream: TextIO, *, enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return
    logger = logging.getLogger("labctl")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("DEBUG %(name)s: %(message)s"))
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


class ApplicationLike(Protocol):
    def execute(self, args: argparse.Namespace) -> CommandResult: ...


class ParseError(ValueError):
    """An argparse error that can be rendered consistently by main()."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ParseError(message)


def _globals(parser: argparse.ArgumentParser) -> None:
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    output.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)
    logging = parser.add_mutually_exclusive_group()
    logging.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS)
    logging.add_argument("--debug", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument(
        "--allow-definition-override", action="store_true", default=argparse.SUPPRESS
    )
    parser.add_argument("--provider", default=argparse.SUPPRESS)
    parser.add_argument("--uri", default=argparse.SUPPRESS)


def _leaf(subparsers: argparse._SubParsersAction[Parser], name: str) -> Parser:
    parser = subparsers.add_parser(name)
    _globals(parser)
    return parser


def _aliases(
    subparsers: argparse._SubParsersAction[Parser],
    *,
    state_filter: bool = False,
) -> None:
    for name in ("ls", "list"):
        parser = _leaf(subparsers, name)
        if state_filter:
            filters = parser.add_mutually_exclusive_group()
            filters.add_argument("--available", action="store_true")
            filters.add_argument("--active", action="store_true")
        parser.set_defaults(command="ls")


def build_parser() -> Parser:
    parser = Parser(prog="labctl", description="Manage reproducible local learning labs")
    _globals(parser)
    groups = parser.add_subparsers(dest="group", required=True, parser_class=Parser)

    version = _leaf(groups, "version")
    version.set_defaults(command="version")
    completion = _leaf(groups, "completion")
    completion.set_defaults(command="completion")
    completion.add_argument("shell", choices=("bash",))

    lab = groups.add_parser("lab")
    _globals(lab)
    lab_commands = lab.add_subparsers(dest="command", required=True, parser_class=Parser)
    _aliases(lab_commands, state_filter=True)
    create = _leaf(lab_commands, "create")
    create.add_argument("id")
    create.add_argument("--trust-external", action="store_true")
    create.add_argument("--allow-untrusted-image", action="store_true")
    inspect = _leaf(lab_commands, "inspect")
    inspect.add_argument("lab")
    for name in ("start", "grade"):
        command = _leaf(lab_commands, name)
        command.add_argument("lab")
    for name in ("stop", "restart", "reset", "rm"):
        command = _leaf(lab_commands, name)
        command.add_argument("lab")
        command.add_argument("--force", action="store_true")
    reconcile = _leaf(lab_commands, "reconcile")
    reconcile.add_argument("lab")
    reconcile.add_argument("--repair", action="store_true")
    ssh = _leaf(lab_commands, "ssh")
    ssh.add_argument("lab")
    ssh.add_argument("vm", nargs="?")
    console = _leaf(lab_commands, "console")
    console.add_argument("lab")
    console.add_argument("vm", nargs="?")

    vm = groups.add_parser("vm")
    _globals(vm)
    vm_commands = vm.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("ls", "list"):
        command = _leaf(vm_commands, name)
        command.add_argument("--lab", dest="lab_id")
        command.set_defaults(command="ls")
    for name in ("inspect", "start"):
        command = _leaf(vm_commands, name)
        command.add_argument("lab_id")
        command.add_argument("vm_name")
    for name in ("stop", "restart", "rm"):
        command = _leaf(vm_commands, name)
        command.add_argument("lab_id")
        command.add_argument("vm_name")
        command.add_argument("--force", action="store_true")
    ssh = _leaf(vm_commands, "ssh")
    ssh.add_argument("lab_id")
    ssh.add_argument("vm_name")
    console = _leaf(vm_commands, "console")
    console.add_argument("lab_id")
    console.add_argument("vm_name")

    image = groups.add_parser("image")
    _globals(image)
    image_commands = image.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("ls", "list"):
        command = _leaf(image_commands, name)
        command.add_argument("--cached", action="store_true")
        command.add_argument("--available", action="store_true")
        command.set_defaults(command="ls")
    pull = _leaf(image_commands, "pull")
    pull.add_argument("image")
    pull.add_argument("--force", action="store_true")
    import_image = _leaf(image_commands, "import")
    import_image.add_argument("image")
    import_image.add_argument("source")
    import_image.add_argument("--checksum")
    inspect = _leaf(image_commands, "inspect")
    inspect.add_argument("image")
    remove = _leaf(image_commands, "rm")
    remove.add_argument("image")
    remove.add_argument("--force", action="store_true")

    provider = groups.add_parser("provider")
    _globals(provider)
    provider_commands = provider.add_subparsers(dest="command", required=True, parser_class=Parser)
    _aliases(provider_commands)
    inspect = _leaf(provider_commands, "inspect")
    inspect.add_argument("provider_id")
    doctor = _leaf(provider_commands, "doctor")
    doctor.add_argument("provider_id", nargs="?")
    for singular, commands in (
        ("lab", lab_commands),
        ("vm", vm_commands),
        ("image", image_commands),
        ("provider", provider_commands),
    ):
        alias = groups.add_parser(
            singular + "s",
            parents=[commands.choices["ls"]],
            add_help=False,
            help=f"alias for {singular} ls",
        )
        alias.set_defaults(group=singular, command="ls")
    return parser


def _validate(args: argparse.Namespace) -> None:
    if args.json and args.quiet:
        raise ParseError("--json and --quiet are mutually exclusive")
    if args.verbose and args.debug:
        raise ParseError("--verbose and --debug are mutually exclusive")
    if args.command in {"ssh", "console"} and args.json:
        raise ParseError("--json is not supported for interactive ssh or console commands")
    if args.group == "vm" and args.command == "rm" and not args.force:
        raise ParseError("vm rm requires --force")
    digest = getattr(args, "checksum", None)
    if digest is not None and (
        len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ParseError("--checksum must be 64 lowercase hexadecimal characters")


def _normalize_globals(argv: Sequence[str]) -> list[str]:
    """Move globals before subcommands while preserving an SSH command after ``--``."""
    values = {"--provider", "--uri"}
    flags = {"--json", "--quiet", "--debug", "--allow-definition-override", "--verbose"}
    global_arguments: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            remaining.extend(argv[index:])
            break
        if argument in values:
            if index + 1 >= len(argv):
                raise ParseError(f"argument {argument}: expected one argument")
            global_arguments.extend((argument, argv[index + 1]))
            index += 2
            continue
        if any(argument.startswith(f"{name}=") for name in values):
            global_arguments.append(argument)
            index += 1
            continue
        if argument in flags or (
            argument.startswith("-") and len(argument) > 1 and set(argument[1:]) == {"v"}
        ):
            global_arguments.append(argument)
        else:
            remaining.append(argument)
        index += 1
    return [*global_arguments, *remaining]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(_normalize_globals(arguments))
    for name, default in (
        ("json", False),
        ("quiet", False),
        ("verbose", 0),
        ("debug", False),
        ("allow_definition_override", False),
        ("provider", None),
        ("uri", None),
    ):
        if not hasattr(args, name):
            setattr(args, name, default)
    _validate(args)
    return args


def _json_success(result: CommandResult) -> str:
    return json.dumps(
        {"schema_version": 1, "ok": True, "kind": result.kind, "data": result.data},
        sort_keys=True,
    )


def _help_parser(arguments: Sequence[str]) -> Parser:
    parser = build_parser()
    current = parser
    skip_value = False
    for argument in _normalize_globals(arguments):
        if skip_value:
            skip_value = False
            continue
        if argument in {"--provider", "--uri"}:
            skip_value = True
            continue
        if argument.startswith("-"):
            continue
        subparsers = next(
            (
                action
                for action in current._actions
                if isinstance(action, argparse._SubParsersAction)
            ),
            None,
        )
        if subparsers is not None and argument in subparsers.choices:
            current = subparsers.choices[argument]
    return current


def _json_error(message: str, status: ExitStatus, details: object | None = None) -> str:
    error: dict[str, object] = {"code": status.name.lower(), "message": message}
    if details is not None:
        error["details"] = details
    return json.dumps(
        {"schema_version": 1, "ok": False, "error": error},
        sort_keys=True,
    )


def _human(result: CommandResult, *, color: bool = False) -> str:
    if not result.data:
        return ""
    if isinstance(result.data, dict):
        if result.kind == "grade":
            lines = result.data.get("stdout", [])
            if isinstance(lines, list):
                rendered = [str(line) for line in lines]
                if color:
                    rendered = [
                        f"\033[32mPASS\033[0m{line[4:]}"
                        if line.startswith("PASS ")
                        else f"\033[31mFAIL\033[0m{line[4:]}"
                        if line.startswith("FAIL ")
                        else line
                        for line in rendered
                    ]
                return "\n".join(rendered)
        return "\n".join(f"{key}: {value}" for key, value in result.data.items())
    if not result.columns:
        return "\n".join(str(item) for item in result.data)
    rows = [[str(item.get(column, "")) for column in result.columns] for item in result.data]
    widths = [
        max(len(column.upper()), *(len(row[index]) for row in rows))
        for index, column in enumerate(result.columns)
    ]
    lines = [
        "  ".join(
            column.upper().ljust(widths[index]) for index, column in enumerate(result.columns)
        ).rstrip()
    ]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()
        for row in rows
    )
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    application: ApplicationLike | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    json_requested = "--json" in arguments
    if json_requested and any(argument in {"-h", "--help"} for argument in arguments):
        result = CommandResult("help", {"text": _help_parser(arguments).format_help()})
        print(_json_success(result), file=output)
        return ExitStatus.SUCCESS
    try:
        args = parse_args(arguments)
        if args.group == "completion":
            if not args.quiet:
                script = bash_completion(build_parser())
                print(
                    _json_success(
                        CommandResult("completion", {"shell": args.shell, "text": script})
                    )
                    if args.json
                    else script,
                    file=output,
                    end="\n" if args.json else "",
                )
            return ExitStatus.SUCCESS
        if args.group == "version":
            if not args.quiet:
                print(
                    _json_success(CommandResult("version", {"version": __version__}))
                    if args.json
                    else f"labctl {__version__}",
                    file=output,
                )
            return ExitStatus.SUCCESS
        show_progress = not args.json and not args.quiet and not args.debug and output.isatty()
        with (
            _debug_logging(errors, enabled=args.debug and not args.json and not args.quiet),
            _spinner(
                output,
                f"Creating lab {getattr(args, 'id', '')}...",
                enabled=show_progress and args.group == "lab" and args.command == "create",
            ),
            _download_progress(
                output,
                f"Pulling {getattr(args, 'image', '')}",
                enabled=show_progress and args.group == "image" and args.command == "pull",
            ) as progress,
        ):
            args.download_progress = progress
            result = (application or Application()).execute(args)
        if not args.json:
            for diagnostic in result.diagnostics:
                print(diagnostic, file=errors)
        if result.status:
            try:
                status = ExitStatus(result.status)
            except ValueError:
                status = ExitStatus.OPERATION
            message = (
                "grading checks failed"
                if result.kind == "grade"
                else "mandatory provider checks failed"
                if result.kind == "doctor"
                else f"{result.kind} command failed"
            )
            if args.json:
                print(_json_error(message, status, result.data), file=errors)
                return result.status
        if not args.quiet:
            if args.json:
                rendered = _json_success(result)
            elif (
                not result.status
                and args.group == "lab"
                and args.command == "create"
                and not args.verbose
                and not args.debug
            ):
                lab_id = (
                    result.data.get("id", args.id) if isinstance(result.data, dict) else args.id
                )
                rendered = f"Lab {lab_id} created."
            elif (
                not result.status
                and args.group == "vm"
                and args.command in {"start", "stop"}
                and not args.verbose
                and not args.debug
            ):
                verb = "started" if args.command == "start" else "stopped"
                rendered = f"VM {args.lab_id}/{args.vm_name} {verb}."
            else:
                rendered = _human(
                    result,
                    color=output.isatty() and "NO_COLOR" not in os.environ,
                )
            if rendered:
                print(rendered, file=output)
        if result.status:
            print(f"error: {message}", file=errors)
        return result.status
    except ParseError as exc:
        status = ExitStatus.USAGE
        message = str(exc)
    except ConfigError as exc:
        status = ExitStatus.USAGE
        message = str(exc)
    except LabctlError as exc:
        status = exc.status
        message = exc.message
    except (OSError, RuntimeError, ValueError) as exc:
        status = ExitStatus.OPERATION
        message = str(exc)
    print(_json_error(message, status) if json_requested else f"error: {message}", file=errors)
    return status
