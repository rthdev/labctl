"""Execute the read-only service-proof logic with process/file evidence, never Podman."""

from __future__ import annotations

import json
import shlex
import subprocess
from types import ModuleType
from typing import Any

import pytest
import test_container_review as review


def probe(lab_id: str) -> ModuleType:
    path = review.catalog.LABS / lab_id / "service_probe.py"
    result = ModuleType("service_probe")
    exec(compile(path.read_text(), str(path), "exec"), result.__dict__)  # noqa: S102
    return result


def state(lab_id: str) -> tuple[tuple[str, ...], dict[tuple[str, ...], str]]:
    container, mounted, source, endpoint, expected = (
        (
            "ct301-web",
            "/etc/ct301/app.conf",
            "/home/student/ct301/app.conf",
            "config",
            "mode=training\n",
        )
        if lab_id == "CT301"
        else ("ct402-api", "/srv/health", "/volume/health", "health", "CT402 recovered API\n")
    )
    prefix = ("exec", container)
    values = {
        ("unshare", "readlink", "-f", "--", source): source + "\n",
        ("unshare", "cat", "--", source): expected,
        ("unshare", "stat", "-Lc", "%d:%i", "--", source): "42:101\n",
        (*prefix, "cat", mounted): expected,
        (*prefix, "stat", "-Lc", "%d:%i", "--", mounted): "42:101\n",
        (
            *prefix,
            "cat",
            "/proc/net/tcp",
            "/proc/net/tcp6",
        ): "sl local_address rem_address st tx_queue tr retrnsmt uid timeout inode\n"
        "0: 00000000:1F90 00000000:0000 0A 0:0 0:0 0 0 0 777\n",
        ("top", container, "pid"): "PID\n1\n",
        (*prefix, "cat", "/proc/1/cmdline"): "httpd\0-f\0-p\08080\0-h\0/srv\0",
        (*prefix, "readlink", "/proc/1/exe"): "/bin/busybox\n",
        (*prefix, "readlink", "/proc/1/cwd"): "/srv\n",
        (
            *prefix,
            "sh",
            "-ec",
            'test ! -e /etc/httpd.conf; test ! -e "$1/httpd.conf"; test ! -e "$2/httpd.conf"',
            "sh",
            "/srv",
            "/srv",
        ): "",
        (*prefix, "stat", "-Lc", "%d:%i", "--", "/srv/" + endpoint): "42:101\n",
        (*prefix, "stat", "-Lc", "%d:%i", "--", "/proc/1/cwd/" + endpoint): "42:101\n",
        (
            *prefix,
            "sh",
            "-ec",
            'for f in /proc/"$1"/fd/*; do readlink "$f" || :; done',
            "sh",
            "1",
        ): "/dev/null\nsocket:[777]\n",
    }
    return (container, mounted, source, endpoint, expected), values


@pytest.mark.parametrize("lab_id", ["CT301", "CT402"])
@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "checker-server",
        "static-copy",
        "wrong-source",
        "empty-source",
        "shadow-mount",
        "extra-listener",
    ],
)
def test_service_proof_connected_outcomes(
    monkeypatch: pytest.MonkeyPatch, lab_id: str, fault: str
) -> None:
    m = probe(lab_id)
    args, values = state(lab_id)
    container, mounted, source, endpoint, _ = args
    prefix = ("exec", container)
    if fault == "checker-server":
        # A listener exists in the shared network but web's sole process sleeps.
        values[(*prefix, "cat", "/proc/1/cmdline")] = "sleep\0infinity\0"
    elif fault == "static-copy":
        values[(*prefix, "stat", "-Lc", "%d:%i", "--", "/proc/1/cwd/" + endpoint)] = "42:202\n"
    elif fault in {"wrong-source", "empty-source"}:
        values[("unshare", "cat", "--", source)] = "wrong\n" if fault == "wrong-source" else ""
    elif fault == "shadow-mount":
        values[(*prefix, "stat", "-Lc", "%d:%i", "--", mounted)] = "42:999\n"
    elif fault == "extra-listener":
        values[(*prefix, "cat", "/proc/net/tcp", "/proc/net/tcp6")] += (
            "1: 0100007F:1F90 00000000:0000 0A 0:0 0:0 0 0 0 888\n"
        )

    def fake(*command: str) -> str:
        return values[command]

    monkeypatch.setattr(m, "run", fake)
    assert m.verify(*args) is (fault == "none")


@pytest.mark.parametrize("lab_id", ["CT301", "CT402"])
@pytest.mark.parametrize("cmdline", ["httpd\0-p\08080\0", "busybox\0httpd\0-p\08080\0-h\0.\0"])
def test_service_proof_accepts_equivalent_commands(
    monkeypatch: pytest.MonkeyPatch, lab_id: str, cmdline: str
) -> None:
    m = probe(lab_id)
    args, values = state(lab_id)
    values[("exec", args[0], "cat", "/proc/1/cmdline")] = cmdline
    monkeypatch.setattr(m, "run", lambda *command: values[command])
    assert m.verify(*args)


@pytest.mark.parametrize("lab_id", ["CT301", "CT402"])
def test_service_command_executes_guest_code(monkeypatch: pytest.MonkeyPatch, lab_id: str) -> None:
    command = shlex.split(review.module(lab_id).SERVICE_COMMAND)
    assert command[:2] == ["/usr/bin/python3", "-c"]
    _args, values = state(lab_id)
    values[("volume", "inspect", "ct402-data")] = json.dumps([{"Mountpoint": "/volume"}])
    calls = []

    def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert argv[0] == "/usr/bin/podman"
        assert kwargs == {"check": True, "capture_output": True, "timeout": 2}
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, values[tuple(argv[1:])].encode(), b"")

    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(SystemExit) as exited:
        exec(compile(command[2], "guest-probe", "exec"), {})  # noqa: S102
    assert exited.value.code == 0
    assert calls


@pytest.mark.parametrize("lab_id", ["CT301", "CT402"])
@pytest.mark.parametrize("failure", ["missing-data", "custom-config", "proc-denied", "timeout"])
def test_service_proof_command_failures_fail_closed(
    monkeypatch: pytest.MonkeyPatch, lab_id: str, failure: str
) -> None:
    m = probe(lab_id)
    args, values = state(lab_id)

    def fake(*command: str) -> str:
        fail = (
            (failure == "missing-data" and command[:2] == ("unshare", "cat"))
            or (failure == "custom-config" and "httpd.conf" in " ".join(command))
            or (failure == "proc-denied" and "/proc/1/cmdline" in command)
        )
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 2)
        if fail:
            raise subprocess.CalledProcessError(1, command)
        return values[command]

    monkeypatch.setattr(m, "run", fake)
    with pytest.raises(SystemExit) as exited:
        m.main(*args)
    assert exited.value.code == 1


@pytest.mark.parametrize("lab_id", ["CT301", "CT402"])
def test_probe_total_deadline_fails_closed(monkeypatch: pytest.MonkeyPatch, lab_id: str) -> None:
    import time

    m = probe(lab_id)
    ticks = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: ticks[0])

    def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        ticks[0] += 1.6
        return subprocess.CompletedProcess(argv, 0, b"output")

    def slow(*args: str) -> bool:
        for _ in range(10):
            m.run("read-only-fixture")
        return True

    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(m, "verify", slow)
    with pytest.raises(SystemExit) as exited:
        m.main("fixture", "/mounted", "/source", "endpoint", "expected")
    assert exited.value.code == 1


@pytest.mark.parametrize("lab_id", ["CT301", "CT402"])
def test_probe_does_not_normalize_file_bytes(monkeypatch: pytest.MonkeyPatch, lab_id: str) -> None:
    m = probe(lab_id)

    def fake(argv: list[str], **kwargs: Any) -> Any:
        output = "mode=training\n" if kwargs.get("text") else b"mode=training\r\n"
        return subprocess.CompletedProcess(argv, 0, output)

    monkeypatch.setattr(subprocess, "run", fake)
    assert m.run("unshare", "cat", "--", "/fixture") == "mode=training\r\n"


@pytest.mark.parametrize("lab_id", ["CT301", "CT402"])
def test_service_proof_uses_live_docroot_not_startup_argument(
    monkeypatch: pytest.MonkeyPatch, lab_id: str
) -> None:
    m = probe(lab_id)
    args, values = state(lab_id)
    prefix = ("exec", args[0])
    values[(*prefix, "readlink", "/proc/1/cwd")] = "/elsewhere\n"
    values[(*prefix, "stat", "-Lc", "%d:%i", "--", "/proc/1/cwd/" + args[3])] = "42:999\n"
    values[
        (
            *prefix,
            "sh",
            "-ec",
            'test ! -e /etc/httpd.conf; test ! -e "$1/httpd.conf"; test ! -e "$2/httpd.conf"',
            "sh",
            "/srv",
            "/elsewhere",
        )
    ] = ""
    monkeypatch.setattr(m, "run", lambda *command: values[command])
    assert not m.verify(*args)


@pytest.mark.parametrize("lab_id", ["CT301", "CT402"])
def test_service_proof_rejects_backing_file_redirect(
    monkeypatch: pytest.MonkeyPatch, lab_id: str
) -> None:
    m = probe(lab_id)
    args, values = state(lab_id)
    values[("unshare", "readlink", "-f", "--", args[2])] = "/elsewhere/copied-data\n"
    monkeypatch.setattr(m, "run", lambda *command: values[command])
    assert not m.verify(*args)


@pytest.mark.parametrize("lab_id", ["CT301", "CT402"])
def test_service_proof_refuses_custom_httpd_config(
    monkeypatch: pytest.MonkeyPatch, lab_id: str
) -> None:
    m = probe(lab_id)
    args, values = state(lab_id)
    values[("exec", args[0], "cat", "/proc/1/cmdline")] = "httpd\0-p\08080\0-c\0/redirects\0"
    monkeypatch.setattr(m, "run", lambda *command: values[command])
    assert not m.verify(*args)
