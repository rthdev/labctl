"""A small, injectable subprocess boundary that never invokes a shell."""

from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured result of an external command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    """An external command returned a non-zero status."""

    def __init__(self, result: CommandResult) -> None:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        super().__init__(f"command {result.argv!r} failed ({result.returncode}): {detail}")
        self.result = result


class CommandRunner(Protocol):
    """Structural type used by provider and image operations."""

    def run(self, argv: list[str], *, check: bool = True) -> CommandResult: ...


class Runner:
    """Run explicit argument vectors with captured text output."""

    def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(part, str) for part in argv)
        ):
            raise TypeError("command must be a non-empty string argument list")
        logger.debug("executing command: %s", shlex.join(argv))
        completed = subprocess.run(argv, check=False, capture_output=True, text=True)  # noqa: S603
        result = CommandResult(
            tuple(argv), completed.returncode, completed.stdout, completed.stderr
        )
        if check and result.returncode:
            raise CommandError(result)
        return result
