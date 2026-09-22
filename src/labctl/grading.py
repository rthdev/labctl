"""Isolated grader context and subprocess execution."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

GRADER_OUTPUT_LIMIT = 1024 * 1024
_CONTAINMENT_LOCK = threading.Lock()

CONTEXT_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "lab_id",
        "title",
        "goal",
        "provider",
        "provider_uri",
        "hosts",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "lab_id": {"type": "string", "minLength": 1},
        "title": {"type": "string", "minLength": 1},
        "goal": {"type": "string", "minLength": 1},
        "provider": {"type": "string", "minLength": 1},
        "provider_uri": {"type": "string", "minLength": 1},
        "hosts": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "name",
                    "hostname",
                    "address",
                    "ssh_user",
                    "private_key_path",
                    "known_hosts_path",
                    "provider",
                    "provider_uri",
                    "lifecycle_state",
                ],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "hostname": {"type": "string", "minLength": 1},
                    "address": {"type": "string", "minLength": 1},
                    "ssh_user": {"type": "string", "minLength": 1},
                    "private_key_path": {"type": "string", "minLength": 1},
                    "known_hosts_path": {"type": "string", "minLength": 1},
                    "provider": {"type": "string", "minLength": 1},
                    "provider_uri": {"type": "string", "minLength": 1},
                    "lifecycle_state": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}


class GradeOutcome(StrEnum):
    PASS = auto()
    FAIL = auto()
    ERROR = auto()


@dataclass(frozen=True)
class GradeResult:
    outcome: GradeOutcome
    exit_code: int
    stdout_lines: tuple[str, ...]
    stderr_lines: tuple[str, ...]
    timed_out: bool = False
    output_overflow: bool = False


class GraderProcess(Protocol):
    returncode: int

    def communicate(self, timeout: float | None = None) -> tuple[str | None, str | None]: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[..., GraderProcess]


def _set_child_subreaper(enabled: bool) -> bool | None:
    """Set Linux subreaper state and return its previous value when supported."""
    if not hasattr(os, "waitpid") or not Path("/proc").exists():
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    current = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(current), 0, 0, 0) != 0:  # PR_GET_CHILD_SUBREAPER
        return None
    previous = bool(current.value)
    if previous != enabled and libc.prctl(36, int(enabled), 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        return None
    return previous


def _direct_children(pid: int) -> set[int]:
    path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        content = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise RuntimeError("grader process containment is unavailable") from exc
    return {int(value) for value in content.split()} if content else set()


def _terminate_and_reap_process_tree(process: GraderProcess, baseline: set[int]) -> None:
    """Kill the process group plus descendants adopted through Linux subreaping."""
    pid = getattr(process, "pid", None)
    if isinstance(pid, int):
        with suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        adopted = _direct_children(os.getpid()) - baseline
        descendants = set(adopted)
        pending = list(adopted)
        while pending:
            child = pending.pop()
            try:
                discovered = _direct_children(child) - descendants
            except RuntimeError:
                discovered = set()
            descendants.update(discovered)
            pending.extend(discovered)
        for child in sorted(descendants, reverse=True):
            with suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)
        for child in adopted:
            with suppress(ChildProcessError):
                os.waitpid(child, os.WNOHANG)
        if not (_direct_children(os.getpid()) - baseline):
            return
        time.sleep(0.01)
    if _direct_children(os.getpid()) - baseline:
        raise RuntimeError("grader process containment could not terminate all descendants")


def _drain_bounded(descriptor: int, destination: Path, limit: int, sizes: list[int]) -> None:
    """Drain one child pipe without allowing its on-disk capture to exceed limit."""
    total = 0
    with os.fdopen(descriptor, "rb") as source, destination.open("wb") as target:
        while chunk := source.read(65536):
            total += len(chunk)
            remaining = limit - target.tell()
            if remaining > 0:
                target.write(chunk[:remaining])
    sizes.append(total)


def _context_document(context: Mapping[str, object]) -> dict[str, object]:
    document = {"schema_version": 1, **context}
    errors = sorted(Draft202012Validator(CONTEXT_SCHEMA_V1).iter_errors(document), key=str)
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "context"
        raise ValueError(f"invalid grading context at {location}: {error.message}")
    return document


def run_grader(
    command: Sequence[str],
    context: Mapping[str, object],
    *,
    timeout: float,
    temp_root: Path | None = None,
    popen: ProcessFactory = subprocess.Popen,  # type: ignore[assignment]
    output_limit: int = GRADER_OUTPUT_LIMIT,
) -> GradeResult:
    """Run a grader with protected context and bounded, file-backed output."""
    if not command:
        raise ValueError("grader command must not be empty")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if output_limit <= 0:
        raise ValueError("output_limit must be positive")
    document = _context_document(context)
    directory = Path(tempfile.mkdtemp(prefix="labctl-grade-", dir=temp_root))
    os.chmod(directory, 0o700)
    context_path = directory / "context.json"
    process: GraderProcess | None = None
    previous_subreaper: bool | None = None
    _CONTAINMENT_LOCK.acquire()
    try:
        previous_subreaper = _set_child_subreaper(True)
        if previous_subreaper is None:
            raise RuntimeError("grader process containment is unavailable")
        baseline_children = _direct_children(os.getpid())
        descriptor = os.open(context_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as context_file:
            json.dump(document, context_file, sort_keys=True)
            context_file.write("\n")
        environment = {
            "LABCTL_CONTEXT": str(context_path),
            "LABCTL_SCHEMA_VERSION": "1",
            "LABCTL_SSH": shutil.which("ssh") or "/usr/bin/ssh",
        }
        stdout_path = directory / "stdout"
        stderr_path = directory / "stderr"
        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        stdout_sizes: list[int] = []
        stderr_sizes: list[int] = []
        stdout_thread = threading.Thread(
            target=_drain_bounded,
            args=(stdout_read, stdout_path, output_limit, stdout_sizes),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_bounded,
            args=(stderr_read, stderr_path, output_limit, stderr_sizes),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            process = popen(
                [*command, str(context_path)],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout_write,
                stderr=stderr_write,
                text=True,
                start_new_session=True,
            )
        finally:
            os.close(stdout_write)
            os.close(stderr_write)
        try:
            timed_out = False
            try:
                returned_stdout, returned_stderr = process.communicate(timeout=timeout)
            except (subprocess.TimeoutExpired, TimeoutError):
                timed_out = True
                pid = getattr(process, "pid", None)
                if isinstance(pid, int):
                    with suppress(ProcessLookupError):
                        os.killpg(pid, signal.SIGKILL)
                else:
                    process.kill()
                _terminate_and_reap_process_tree(process, baseline_children)
                returned_stdout, returned_stderr = process.communicate()
        finally:
            _terminate_and_reap_process_tree(process, baseline_children)
            stdout_thread.join()
            stderr_thread.join()
        stdout_bytes = stdout_path.read_bytes()
        stderr_bytes = stderr_path.read_bytes()
        stdout_size = stdout_sizes[0]
        stderr_size = stderr_sizes[0]
        if isinstance(returned_stdout, str):
            encoded = returned_stdout.encode()
            stdout_size = len(encoded)
            stdout_bytes = encoded[:output_limit]
        if isinstance(returned_stderr, str):
            encoded = returned_stderr.encode()
            stderr_size = len(encoded)
            stderr_bytes = encoded[:output_limit]
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        output_overflow = stdout_size > output_limit or stderr_size > output_limit
        if timed_out or output_overflow:
            outcome = GradeOutcome.ERROR
        elif process.returncode == 0:
            outcome = GradeOutcome.PASS
        elif process.returncode == 1:
            outcome = GradeOutcome.FAIL
        else:
            outcome = GradeOutcome.ERROR
        return GradeResult(
            outcome=outcome,
            exit_code=process.returncode,
            stdout_lines=tuple(stdout.splitlines()),
            stderr_lines=tuple(stderr.splitlines()),
            timed_out=timed_out,
            output_overflow=output_overflow,
        )
    finally:
        if previous_subreaper is not None:
            _set_child_subreaper(previous_subreaper)
        _CONTAINMENT_LOCK.release()
        shutil.rmtree(directory)
