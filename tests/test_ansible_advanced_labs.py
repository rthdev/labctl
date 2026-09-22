from __future__ import annotations

import base64
import importlib.util
import json
import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from labctl.definitions import load_definition

ROOT = Path(__file__).parents[1]
LABS = ROOT / "src/labctl/data/labs"
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureGradeKey student@controller"


@pytest.mark.parametrize(
    ("lab_id", "title", "vm_names", "reset_vms"),
    [
        (
            "AN401",
            "Repair fragile Ansible application automation",
            ["target", "controller"],
            ("target",),
        ),
        (
            "AN402",
            "Recover a multi-node service platform with Ansible",
            ["web1", "web2", "web3", "controller"],
            ("web1", "web2", "web3"),
        ),
    ],
)
def test_advanced_lab_definition_topology_and_assets(
    lab_id: str, title: str, vm_names: list[str], reset_vms: tuple[str, ...]
) -> None:
    definition = load_definition(LABS / lab_id / "lab.yaml")

    assert definition.id == lab_id
    assert definition.title == title
    assert definition.provider == "kvm"
    assert [vm.name for vm in definition.vms] == vm_names
    assert definition.grading.reset_vms == reset_vms
    assert all(vm.ssh_user == "student" for vm in definition.vms)
    assert os.access(definition.grader, os.X_OK)
    assert all(os.access(vm.setup, os.X_OK) for vm in definition.vms)

    controller = next(vm for vm in definition.vms if vm.name == "controller")
    project_setup = controller.setup.read_text(encoding="utf-8")
    assert "/home/student/ansible-lab/site.yml" in project_setup
    assert "/home/student/ansible-lab/roles" in project_setup


def _fake_ssh(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "ssh-argv.jsonl"
    executable = tmp_path / "ssh-fake"
    executable.write_text(
        """#!/usr/bin/python3
import json
import os
import sys

command = sys.argv[-1]
with open(os.environ["FAKE_SSH_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
answers = json.loads(os.environ["FAKE_SSH_ANSWERS"])
answer = answers.get(f"{sys.argv[-2]}|{command}", answers.get(command))
if answer is None:
    answer = next(
        (
            value
            for key, value in answers.items()
            if key.endswith("*") and command.startswith(key[:-1])
        ),
        None,
    )
state_path = os.environ["FAKE_SSH_STATE"]
try:
    counts = json.loads(open(state_path, encoding="utf-8").read())
except FileNotFoundError:
    counts = {}
index = counts.get(command, 0)
counts[command] = index + 1
with open(state_path, "w", encoding="utf-8") as stream:
    json.dump(counts, stream)
if isinstance(answer, list):
    answer = answer[min(index, len(answer) - 1)]
if answer is None:
    raise SystemExit(255)
if isinstance(answer, dict):
    sys.stdout.write(answer.get("stdout", ""))
    raise SystemExit(answer.get("returncode", 0))
sys.stdout.write(str(answer))
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable, log


def _context(tmp_path: Path, lab_id: str, host_names: tuple[str, ...]) -> Path:
    identity = tmp_path / "management-identity"
    identity.write_text("fixture, not a key\n", encoding="utf-8")
    addresses = {name: f"192.0.2.{10 + index}" for index, name in enumerate(host_names)}
    known_hosts: dict[str, Path] = {}
    for name, address in addresses.items():
        path = tmp_path / f"known_hosts-{name}"
        path.write_text(f"{address} ssh-ed25519 fixture-{name}\n", encoding="utf-8")
        known_hosts[name] = path
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "lab_id": lab_id,
                "hosts": {
                    name: {
                        "address": addresses[name],
                        "ssh_user": "student",
                        "private_key_path": str(identity),
                        "known_hosts_path": str(known_hosts[name]),
                    }
                    for name in host_names
                },
            }
        ),
        encoding="utf-8",
    )
    return context


def _run_grader(
    tmp_path: Path, lab_id: str, host_names: tuple[str, ...], answers: dict[str, object]
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    definition = load_definition(LABS / lab_id / "lab.yaml")
    ssh, log = _fake_ssh(tmp_path)
    context = _context(tmp_path, lab_id, host_names)
    result = subprocess.run(
        [definition.grader, context],
        env={
            "LABCTL_SSH": str(ssh),
            "FAKE_SSH_LOG": str(log),
            "FAKE_SSH_STATE": str(tmp_path / "ssh-state.json"),
            "FAKE_SSH_ANSWERS": json.dumps(answers),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    calls = (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )
    return result, calls


def _recap(hosts: tuple[str, ...], *, changed: int = 0, failed: int = 0) -> str:
    return "PLAY RECAP ****\n" + "".join(
        f"{host} : ok=8 changed={changed} unreachable=0 failed={failed} "
        "skipped=0 rescued=0 ignored=0\n"
        for host in hosts
    )


def _base_answers(lab_id: str, play_hosts: tuple[str, ...]) -> dict[str, object]:
    playbook = (
        f"/usr/bin/env ANSIBLE_CONFIG=/tmp/labctl-{lab_id.lower()}-grade/ansible.cfg "
        "ANSIBLE_STDOUT_CALLBACK=default "
        f"ANSIBLE_CALLBACK_PLUGINS=/tmp/labctl-{lab_id.lower()}-grade/callback_plugins "
        "ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0 /usr/bin/ansible-playbook "
        f"-i /tmp/labctl-{lab_id.lower()}-grade/inventory.ini "
        "/home/student/ansible-lab/site.yml"
    )
    return {
        "umask 077; *": "",
        f"/usr/bin/cat -- /home/student/.ssh/id_{lab_id.lower()}_grade.pub": PUBLIC_KEY,
        "/usr/bin/python3 -c *": "",
        playbook: [_recap(play_hosts, changed=3), _recap(play_hosts)],
        f"/usr/bin/rm -rf -- /tmp/labctl-{lab_id.lower()}-grade": "",
    }


def _an401_answers() -> dict[str, object]:
    answers = _base_answers("AN401", ("target",))
    answers.update(
        {
            "/usr/bin/systemctl is-enabled httpd": "enabled\n",
            "/usr/bin/systemctl is-active httpd": "active\n",
            "/usr/bin/stat -c '%U:%G %a' /etc/an401/app.conf": "root:apache 640\n",
            "/usr/bin/sudo -n /usr/bin/base64 -w0 -- /etc/an401/app.conf": (
                "ZW52aXJvbm1lbnQ9cHJvZHVjdGlvbgo="
            ),
            "/usr/bin/curl -fsS http://127.0.0.1/": "AN401 application ready\n",
        }
    )
    return answers


def test_an401_grades_outcomes_and_two_run_idempotence_without_inspecting_project(
    tmp_path: Path,
) -> None:
    result, calls = _run_grader(tmp_path, "AN401", ("controller", "target"), _an401_answers())

    assert result.returncode == 0, result.stderr
    assert all(line.startswith("PASS AN401: ") for line in result.stdout.splitlines())
    commands = [call[-1] for call in calls]
    playbook = (
        "/usr/bin/env ANSIBLE_CONFIG=/tmp/labctl-an401-grade/ansible.cfg "
        "ANSIBLE_STDOUT_CALLBACK=default "
        "ANSIBLE_CALLBACK_PLUGINS=/tmp/labctl-an401-grade/callback_plugins "
        "ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0 /usr/bin/ansible-playbook "
        "-i /tmp/labctl-an401-grade/inventory.ini /home/student/ansible-lab/site.yml"
    )
    assert commands.count(playbook) == 2
    staging_command = next(
        command
        for command in commands
        if command.startswith("/usr/bin/python3 -c ") and "inventory.ini" in command
    )
    assert base64.b64encode(b"[defaults]\nstdout_callback = default\n").decode() in staging_command
    assert not any("cat -- /home/student/ansible-lab/site.yml" in command for command in commands)
    assert all(str(tmp_path / "management-identity") not in command for command in commands)


@pytest.mark.parametrize("failure", ["playbook", "no-op"], ids=["failed-play", "no-op"])
def test_an401_rejects_failed_or_noop_automation(tmp_path: Path, failure: str) -> None:
    answers = _an401_answers()
    playbook = (
        "/usr/bin/env ANSIBLE_CONFIG=/tmp/labctl-an401-grade/ansible.cfg "
        "ANSIBLE_STDOUT_CALLBACK=default "
        "ANSIBLE_CALLBACK_PLUGINS=/tmp/labctl-an401-grade/callback_plugins "
        "ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0 /usr/bin/ansible-playbook "
        "-i /tmp/labctl-an401-grade/inventory.ini /home/student/ansible-lab/site.yml"
    )
    if failure == "playbook":
        answers[playbook] = {"returncode": 2, "stdout": _recap(("target",), failed=1)}
    else:
        answers["/usr/bin/systemctl is-active httpd"] = {"returncode": 3, "stdout": "inactive\n"}

    result, _calls = _run_grader(tmp_path, "AN401", ("controller", "target"), answers)

    assert result.returncode == 1
    assert "FAIL AN401:" in result.stdout


@pytest.mark.parametrize(
    "recap",
    [
        _recap(("target",), changed=1),
        _recap(("target",), failed=1),
        _recap(("target", "extra")),
        "PLAY RECAP ****\n",
    ],
    ids=["changed", "failed", "extra-host", "missing"],
)
def test_an401_rejects_non_idempotent_or_invalid_second_recap(tmp_path: Path, recap: str) -> None:
    answers = _an401_answers()
    playbook = (
        "/usr/bin/env ANSIBLE_CONFIG=/tmp/labctl-an401-grade/ansible.cfg "
        "ANSIBLE_STDOUT_CALLBACK=default "
        "ANSIBLE_CALLBACK_PLUGINS=/tmp/labctl-an401-grade/callback_plugins "
        "ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0 /usr/bin/ansible-playbook "
        "-i /tmp/labctl-an401-grade/inventory.ini /home/student/ansible-lab/site.yml"
    )
    answers[playbook] = [_recap(("target",), changed=2), recap]

    result, _calls = _run_grader(tmp_path, "AN401", ("controller", "target"), answers)

    assert result.returncode == 1
    assert "FAIL AN401: second playbook run reports changed=0 and failures=0" in result.stdout


def _an402_answers() -> dict[str, object]:
    managed = ("web1", "web2", "web3")
    answers = _base_answers("AN402", managed)
    answers.update(
        {
            "/usr/bin/systemctl is-enabled httpd": "enabled\n",
            "/usr/bin/systemctl is-active httpd": "active\n",
            "/usr/bin/systemctl is-enabled an402-backend": "enabled\n",
            "/usr/bin/systemctl is-active an402-backend": "active\n",
            "/usr/bin/sudo -n /usr/bin/base64 -w0 -- /etc/httpd/conf.d/an402.conf": (
                "UHJveHlQYXNzIC9hcGkvIGh0dHA6Ly8xMjcuMC4wLjE6ODA4MC8KUHJveHlQYXNzUmV2ZXJzZSA"
                "vYXBpLyBodHRwOi8vMTI3LjAuMC4xOjgwODAvCg=="
            ),
            "/usr/bin/systemctl is-enabled firewalld": "enabled\n",
            "/usr/bin/firewall-cmd --quiet --query-service=http": "",
            "/usr/bin/firewall-cmd --quiet --permanent --query-service=http": "",
            "/usr/sbin/getenforce": "Enforcing\n",
            "/usr/sbin/getsebool httpd_can_network_connect": ("httpd_can_network_connect --> on\n"),
            "/usr/bin/curl -fsS http://127.0.0.1/api/health": "ok\n",
            "/usr/bin/curl -fsS http://127.0.0.1/": "AN402 platform ready\n",
            "/usr/bin/cat -- /home/student/ansible-lab/recovery-summary.json": json.dumps(
                {
                    "lab": "AN402",
                    "status": "recovered",
                    "hosts": [{"name": name, "status": "healthy"} for name in managed],
                }
            ),
            "/usr/bin/rm -f -- /home/student/ansible-lab/recovery-summary.json": "",
        }
    )
    return answers


def test_an402_grades_every_target_and_controller_summary_without_inspecting_project(
    tmp_path: Path,
) -> None:
    hosts = ("controller", "web1", "web2", "web3")
    result, calls = _run_grader(tmp_path, "AN402", hosts, _an402_answers())

    assert result.returncode == 0, result.stderr
    assert all(line.startswith("PASS AN402: ") for line in result.stdout.splitlines())
    commands = [call[-1] for call in calls]
    playbook = (
        "/usr/bin/env ANSIBLE_CONFIG=/tmp/labctl-an402-grade/ansible.cfg "
        "ANSIBLE_STDOUT_CALLBACK=default "
        "ANSIBLE_CALLBACK_PLUGINS=/tmp/labctl-an402-grade/callback_plugins "
        "ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0 /usr/bin/ansible-playbook "
        "-i /tmp/labctl-an402-grade/inventory.ini /home/student/ansible-lab/site.yml"
    )
    assert commands.count(playbook) == 2
    summary_reset = "/usr/bin/rm -f -- /home/student/ansible-lab/recovery-summary.json"
    assert commands.index(summary_reset) < commands.index(playbook)
    staging_command = next(
        command
        for command in commands
        if command.startswith("/usr/bin/python3 -c ") and "inventory.ini" in command
    )
    target_known_hosts = "".join(
        f"192.0.2.{index} ssh-ed25519 fixture-{name}\n"
        for index, name in ((11, "web1"), (12, "web2"), (13, "web3"))
    ).encode()
    assert base64.b64encode(target_known_hosts).decode() in staging_command
    assert base64.b64encode(b"[defaults]\nstdout_callback = default\n").decode() in staging_command
    assert commands.count("/usr/bin/systemctl is-active httpd") == 3
    assert commands.count("/usr/bin/curl -fsS http://127.0.0.1/api/health") == 3
    assert not any("cat -- /home/student/ansible-lab/site.yml" in command for command in commands)
    assert all(str(tmp_path / "management-identity") not in command for command in commands)


@pytest.mark.parametrize("failure", ["playbook", "no-op", "idempotence", "extra-host"])
def test_an402_rejects_failed_noop_or_non_idempotent_automation(
    tmp_path: Path, failure: str
) -> None:
    answers = _an402_answers()
    playbook = (
        "/usr/bin/env ANSIBLE_CONFIG=/tmp/labctl-an402-grade/ansible.cfg "
        "ANSIBLE_STDOUT_CALLBACK=default "
        "ANSIBLE_CALLBACK_PLUGINS=/tmp/labctl-an402-grade/callback_plugins "
        "ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0 /usr/bin/ansible-playbook "
        "-i /tmp/labctl-an402-grade/inventory.ini /home/student/ansible-lab/site.yml"
    )
    if failure == "playbook":
        answers[playbook] = {"returncode": 2, "stdout": _recap(("web1",), failed=1)}
    elif failure == "no-op":
        answers["student@192.0.2.12|/usr/bin/systemctl is-active httpd"] = {
            "returncode": 3,
            "stdout": "inactive\n",
        }
    elif failure == "idempotence":
        answers[playbook] = [
            _recap(("web1", "web2", "web3"), changed=4),
            _recap(("web1", "web2", "web3"), changed=1),
        ]
    else:
        answers[playbook] = [
            _recap(("web1", "web2", "web3"), changed=4),
            _recap(("web1", "web2", "web3", "extra")),
        ]

    result, _calls = _run_grader(tmp_path, "AN402", ("controller", "web1", "web2", "web3"), answers)

    assert result.returncode == 1
    assert "FAIL AN402:" in result.stdout


def test_an402_requires_stale_summary_removal_before_running_playbook(tmp_path: Path) -> None:
    answers = _an402_answers()
    answers["/usr/bin/rm -f -- /home/student/ansible-lab/recovery-summary.json"] = {
        "returncode": 1,
        "stdout": "",
    }

    result, calls = _run_grader(tmp_path, "AN402", ("controller", "web1", "web2", "web3"), answers)

    assert result.returncode == 1
    assert "FAIL AN402: prior controller recovery summary is invalidated" in result.stdout
    assert not any("/usr/bin/ansible-playbook" in call[-1] for call in calls)


@pytest.mark.parametrize(
    ("command", "description"),
    [
        ("/usr/bin/systemctl is-enabled firewalld", "firewall service is enabled"),
        ("/usr/bin/firewall-cmd --quiet --query-service=http", "runtime firewall"),
        (
            "/usr/bin/firewall-cmd --quiet --permanent --query-service=http",
            "permanent firewall",
        ),
    ],
)
def test_an402_requires_enabled_runtime_and_permanent_firewall(
    tmp_path: Path, command: str, description: str
) -> None:
    answers = _an402_answers()
    answers[command] = {"returncode": 1, "stdout": ""}

    result, _calls = _run_grader(tmp_path, "AN402", ("controller", "web1", "web2", "web3"), answers)

    assert result.returncode == 1
    assert description in result.stdout


@pytest.mark.parametrize(
    "summary",
    [
        "not json",
        json.dumps({"lab": "AN402", "status": "recovered", "hosts": []}),
        json.dumps(
            {
                "lab": "AN402",
                "status": "failed",
                "hosts": [{"name": name, "status": "healthy"} for name in ("web1", "web2", "web3")],
            }
        ),
    ],
    ids=["invalid-json", "missing-hosts", "wrong-status"],
)
def test_an402_rejects_invalid_recovery_summary(tmp_path: Path, summary: str) -> None:
    answers = _an402_answers()
    answers["/usr/bin/cat -- /home/student/ansible-lab/recovery-summary.json"] = summary

    result, _calls = _run_grader(tmp_path, "AN402", ("controller", "web1", "web2", "web3"), answers)

    assert result.returncode == 1
    assert "FAIL AN402: controller recovery summary" in result.stdout


@pytest.mark.parametrize("lab_id", ["AN401", "AN402"])
@pytest.mark.parametrize("stale_kind", ["directory", "file", "symlink"])
def test_advanced_graders_stage_without_following_stale_symlinks(
    tmp_path: Path, lab_id: str, stale_kind: str
) -> None:
    grader_path = LABS / lab_id / "grade.py"
    specification = importlib.util.spec_from_file_location(f"{lab_id.lower()}_grade", grader_path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    staging = tmp_path / "staging"
    victim = tmp_path / "victim"
    victim.mkdir()
    sentinel = victim / "inventory.ini"
    sentinel.write_bytes(b"do not overwrite")
    if stale_kind == "symlink":
        staging.symlink_to(victim, target_is_directory=True)
    elif stale_kind == "file":
        staging.write_bytes(b"attacker-controlled stale file")
    else:
        staging.mkdir()
        (staging / "inventory.ini").symlink_to(sentinel)
        (staging / "known_hosts").write_bytes(b"stale")
    module.STAGING_DIRECTORY = str(staging)  # type: ignore[attr-defined]

    command = module._python_write_command(
        {
            str(staging / "inventory.ini"): b"inventory",
            str(staging / "known_hosts"): b"known hosts",
        }
    )
    result = subprocess.run(shlex.split(command), text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert not staging.is_symlink()
    assert stat.S_IMODE(staging.stat().st_mode) == 0o700
    assert (staging / "inventory.ini").read_bytes() == b"inventory"
    assert (staging / "known_hosts").read_bytes() == b"known hosts"
    assert stat.S_IMODE((staging / "inventory.ini").stat().st_mode) == 0o600
    assert sentinel.read_bytes() == b"do not overwrite"
