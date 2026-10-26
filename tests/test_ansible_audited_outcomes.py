from __future__ import annotations

import os
import runpy
import shlex
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from test_ansible_intermediate_labs import _run_grader

LABS = Path(__file__).parents[1] / "src/labctl/data/labs"


def test_an201_restarts_backend_when_probe_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reuse the context fixture but run main in-process to inject a transport exception.
    _run_grader(tmp_path, "AN201", ("app", "proxy", "controller"), {})
    grade = runpy.run_path(str(LABS / "AN201/grade.py"))
    commands: list[str] = []

    def remote(ssh: Path, host: dict[str, str], command: str) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "backend-stopped" in command:
            raise OSError("transport execution failed")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setitem(grade["main"].__globals__, "_remote", remote)
    monkeypatch.setattr("sys.argv", ["grade.py", str(tmp_path / "context.json")])
    with pytest.raises(OSError, match="transport execution failed"):
        grade["main"]()
    assert commands[-2:] == [
        "/usr/bin/sudo -n /usr/bin/systemctl start nginx",
        "/usr/bin/rm -rf -- /tmp/labctl-an201-grade",
    ]


@pytest.mark.parametrize("action", ["stop", "start"])
def test_an201_service_execution_failure_cannot_pass(tmp_path: Path, action: str) -> None:
    answers: dict[str, object] = {
        "/usr/bin/sudo -n /usr/bin/systemctl stop nginx": "",
        "/usr/bin/sudo -n /usr/bin/systemctl start nginx": "",
        "/usr/bin/curl *": {"returncode": 22, "stdout": "502"},
    }
    answers[f"/usr/bin/sudo -n /usr/bin/systemctl {action} nginx"] = {"returncode": 1}
    result, _ = _run_grader(tmp_path, "AN201", ("app", "proxy", "controller"), answers)
    assert "FAIL AN201: proxy depends on the application backend" in result.stdout


@pytest.mark.parametrize("state", ["denied", "readable", "missing", "directory"])
def test_an302_real_permission_probe_never_prints_content(tmp_path: Path, state: str) -> None:
    if os.geteuid() == 0:
        pytest.skip("real DAC denial requires an unprivileged test process")
    grade = runpy.run_path(str(LABS / "AN302/grade.py"))
    argv = shlex.split(grade["DENIAL_COMMAND"])
    assert argv[:5] == ["/usr/bin/sudo", "-n", "-u", "deployer", "/usr/bin/python3"]
    secret = tmp_path / "secret"
    if state == "directory":
        secret.mkdir()
    elif state != "missing":
        secret.write_text("never-display-this-fixture")
        secret.chmod(0 if state == "denied" else 0o600)
    argv[-1] = argv[-1].replace("/etc/an302/secret", str(secret))
    try:
        result = subprocess.run(argv[4:], text=True, capture_output=True, check=False)
    finally:
        if state == "denied":
            secret.chmod(0o600)
    assert (result.returncode == 0) == (state == "denied")
    assert result.stdout == ("AN302_PERMISSION_DENIED\n" if state == "denied" else "")
    assert "never-display-this-fixture" not in result.stdout + result.stderr


@pytest.mark.parametrize("status", [200, 404, 502, 503, 504])
def test_an201_real_curl_backend_response(tmp_path: Path, status: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"response-body-not-a-status")

        def log_message(self, format: str, *args: object) -> None:
            pass

    _, calls = _run_grader(
        tmp_path,
        "AN201",
        ("app", "proxy", "controller"),
        {
            "/usr/bin/sudo -n /usr/bin/systemctl stop nginx": "",
        },
    )
    probe = next(call for call in calls if "backend-stopped" in call[-1])
    assert probe[-2] == "student@192.0.2.12"
    assert "StrictHostKeyChecking=yes" in probe
    argv = shlex.split(probe[-1])
    assert argv[-1] == "http://192.0.2.11/"
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            argv[-1] = f"http://127.0.0.1:{server.server_port}/"
            result = subprocess.run(argv, text=True, capture_output=True, check=False)
        finally:
            server.shutdown()
            thread.join()
    assert result.returncode == (0 if status == 200 else 22)
    assert result.stdout == str(status)


@pytest.mark.parametrize(
    "code,output", [(1, ""), (127, ""), (255, ""), (0, ""), (1, "AN302_PERMISSION_DENIED")]
)
def test_an302_requires_explicit_permission_denial(tmp_path: Path, code: int, output: str) -> None:
    result, _ = _run_grader(
        tmp_path,
        "AN302",
        ("target", "controller"),
        {
            "/usr/bin/sudo -n -u deployer *": {"returncode": code, "stdout": output},
        },
    )
    assert "FAIL AN302: deployer cannot read the secret" in result.stdout


@pytest.mark.parametrize(
    "code,output", [(255, ""), (1, ""), (127, ""), (7, "000"), (28, "000"), (22, "404"), (0, "502")]
)
def test_an201_backend_probe_rejects_unrelated_failures(
    tmp_path: Path, code: int, output: str
) -> None:
    result, calls = _run_grader(
        tmp_path,
        "AN201",
        ("app", "proxy", "controller"),
        {
            "/usr/bin/sudo -n /usr/bin/systemctl stop nginx": "",
            "/usr/bin/sudo -n /usr/bin/systemctl start nginx": "",
            "/usr/bin/curl *": {"returncode": code, "stdout": output},
        },
    )
    assert "FAIL AN201: proxy depends on the application backend" in result.stdout
    assert any(call[-1] == "/usr/bin/sudo -n /usr/bin/systemctl start nginx" for call in calls)


@pytest.mark.parametrize("options", ["loop,noauto", "noauto,defaults,loop", "loop,auto,noauto"])
def test_an202_rejects_non_boot_mount(options: str) -> None:
    grade = runpy.run_path(str(LABS / "AN202/grade.py"))
    assert not grade["_persistent_loop_mount"](
        f"/var/lib/an202/storage.img /srv/an202 ext4 {options} 0 0\n"
    )
