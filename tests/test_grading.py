from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import labctl.grading as grading_module
from labctl.grading import GradeOutcome, run_grader


class FakeProcess:
    def __init__(self, returncode: int = 0, timeout: bool = False) -> None:
        self.returncode = returncode
        self.timeout = timeout
        self.killed = False

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if self.timeout and not self.killed:
            raise TimeoutError
        return ("one\ntwo\n", "warning\n")

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_grader_context_permissions_schema_minimal_env_and_cleanup(tmp_path: Path) -> None:
    observed: dict[str, Any] = {}

    def popen(command: list[str], **kwargs: Any) -> FakeProcess:
        context_path = Path(kwargs["env"]["LABCTL_CONTEXT"])
        observed.update(
            command=command,
            env=kwargs["env"],
            directory_mode=context_path.parent.stat().st_mode & 0o777,
            file_mode=context_path.stat().st_mode & 0o777,
            document=json.loads(context_path.read_text()),
            context_path=context_path,
        )
        return FakeProcess()

    result = run_grader(
        ["/opt/lab/grade"],
        {
            "lab_id": "LX001",
            "title": "Linux lab",
            "goal": "Learn",
            "provider": "kvm",
            "provider_uri": "qemu:///session",
            "hosts": {
                "node1": {
                    "name": "node1",
                    "hostname": "node1.lab",
                    "address": "192.0.2.2",
                    "ssh_user": "student",
                    "private_key_path": "/keys/id",
                    "known_hosts_path": "/keys/known_hosts",
                    "provider": "kvm",
                    "provider_uri": "qemu:///session",
                    "lifecycle_state": "ready",
                }
            },
        },
        timeout=10,
        temp_root=tmp_path,
        popen=popen,
    )

    assert observed["env"].keys() == {"LABCTL_CONTEXT", "LABCTL_SCHEMA_VERSION", "LABCTL_SSH"}
    assert observed["directory_mode"] == 0o700
    assert observed["file_mode"] == 0o600
    assert observed["document"]["schema_version"] == 1
    assert observed["document"]["hosts"]["node1"]["private_key_path"] == "/keys/id"
    assert observed["command"][-1] == str(observed["context_path"])
    assert result.outcome is GradeOutcome.PASS
    assert result.stdout_lines == ("one", "two")
    assert result.stderr_lines == ("warning",)
    assert not Path(observed["context_path"]).exists()


def test_grader_exit_mapping_and_timeout_kills_process(tmp_path: Path) -> None:
    process = FakeProcess(timeout=True)
    result = run_grader(
        ["/grade"],
        {
            "lab_id": "LX001",
            "title": "x",
            "goal": "x",
            "provider": "kvm",
            "provider_uri": "qemu:///session",
            "hosts": {},
        },
        timeout=1,
        temp_root=tmp_path,
        popen=lambda *args, **kwargs: process,
    )
    assert process.killed
    assert result.outcome is GradeOutcome.ERROR
    assert result.timed_out is True

    failed = run_grader(
        ["/grade"],
        {
            "lab_id": "LX001",
            "title": "x",
            "goal": "x",
            "provider": "kvm",
            "provider_uri": "qemu:///session",
            "hosts": {},
        },
        timeout=1,
        temp_root=tmp_path,
        popen=lambda *args, **kwargs: FakeProcess(returncode=1),
    )
    errored = run_grader(
        ["/grade"],
        {
            "lab_id": "LX001",
            "title": "x",
            "goal": "x",
            "provider": "kvm",
            "provider_uri": "qemu:///session",
            "hosts": {},
        },
        timeout=1,
        temp_root=tmp_path,
        popen=lambda *args, **kwargs: FakeProcess(returncode=9),
    )
    assert failed.outcome is GradeOutcome.FAIL
    assert errored.outcome is GradeOutcome.ERROR


def test_grader_rejects_invalid_context_before_starting_process(tmp_path: Path) -> None:
    called = False

    def popen(*args: Any, **kwargs: Any) -> FakeProcess:
        nonlocal called
        called = True
        return FakeProcess()

    try:
        run_grader(
            ["/grade"],
            {
                "lab_id": "LX001",
                "title": "x",
                "goal": "x",
                "provider": "kvm",
                "provider_uri": "qemu:///session",
                "hosts": {},
                "secret": os.environ.get("HOME"),
            },
            timeout=1,
            temp_root=tmp_path,
            popen=popen,
        )
    except ValueError as error:
        assert "secret" in str(error)
    else:
        raise AssertionError("invalid context accepted")
    assert called is False


def test_grader_output_is_bounded_and_overflow_is_explicit(tmp_path: Path) -> None:
    result = run_grader(
        [
            sys.executable,
            "-c",
            "import sys; print('one'); print('two'); "
            "sys.stderr.write('diagnostic-output-too-large\\n')",
        ],
        {
            "lab_id": "LX001",
            "title": "x",
            "goal": "x",
            "provider": "kvm",
            "provider_uri": "qemu:///session",
            "hosts": {},
        },
        timeout=10,
        temp_root=tmp_path,
        output_limit=12,
    )

    assert result.outcome is GradeOutcome.ERROR
    assert result.output_overflow is True
    assert result.stdout_lines == ("one", "two")
    assert result.stderr_lines == ("diagnostic-o",)
    assert list(tmp_path.iterdir()) == []


def test_grader_output_files_are_os_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sizes: dict[str, int] = {}
    real_rmtree = grading_module.shutil.rmtree

    def inspect_then_remove(directory: Path) -> None:
        sizes.update(
            stdout=(directory / "stdout").stat().st_size,
            stderr=(directory / "stderr").stat().st_size,
        )
        real_rmtree(directory)

    monkeypatch.setattr(grading_module.shutil, "rmtree", inspect_then_remove)
    result = run_grader(
        [sys.executable, "-c", "import os; os.write(1, b'x' * 4096)"],
        {
            "lab_id": "LX001",
            "title": "x",
            "goal": "x",
            "provider": "kvm",
            "provider_uri": "qemu:///session",
            "hosts": {},
        },
        timeout=10,
        temp_root=tmp_path,
        output_limit=64,
    )

    assert result.outcome is GradeOutcome.ERROR
    assert result.output_overflow is True
    assert sizes["stdout"] <= 64
    assert sizes["stderr"] <= 64


def test_grader_kills_lingering_process_group_after_main_exits(tmp_path: Path) -> None:
    result = run_grader(
        [
            sys.executable,
            "-c",
            "import subprocess, sys; child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)']); print(child.pid, flush=True)",
        ],
        {
            "lab_id": "LX001",
            "title": "x",
            "goal": "x",
            "provider": "kvm",
            "provider_uri": "qemu:///session",
            "hosts": {},
        },
        timeout=10,
        temp_root=tmp_path,
    )
    child = int(result.stdout_lines[0])
    alive = True
    try:
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.02)
        assert alive is False
    finally:
        if alive:
            os.kill(child, signal.SIGKILL)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux process containment")
def test_grader_kills_double_forked_setsid_descendant(tmp_path: Path) -> None:
    script = (
        "import os, sys, time; first=os.fork(); "
        "(sys.exit(0) if first else None); os.setsid(); second=os.fork(); "
        "(sys.exit(0) if second else None); print(os.getpid(), flush=True); time.sleep(30)"
    )
    result = run_grader(
        [sys.executable, "-c", script],
        {
            "lab_id": "LX001",
            "title": "x",
            "goal": "x",
            "provider": "kvm",
            "provider_uri": "qemu:///session",
            "hosts": {},
        },
        timeout=10,
        temp_root=tmp_path,
    )
    escaped = int(result.stdout_lines[0])

    with pytest.raises(ProcessLookupError):
        os.kill(escaped, 0)


def test_grader_fails_closed_when_subreaper_containment_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def popen(*args: Any, **kwargs: Any) -> FakeProcess:
        nonlocal called
        called = True
        return FakeProcess()

    monkeypatch.setattr(grading_module, "_set_child_subreaper", lambda enabled: None)

    with pytest.raises(RuntimeError, match="containment"):
        run_grader(
            ["/grade"],
            {
                "lab_id": "LX001",
                "title": "x",
                "goal": "x",
                "provider": "kvm",
                "provider_uri": "qemu:///session",
                "hosts": {},
            },
            timeout=1,
            temp_root=tmp_path,
            popen=popen,
        )

    assert called is False
