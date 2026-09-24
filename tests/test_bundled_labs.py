from __future__ import annotations

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
AN001_KEY_COMMAND = (
    "umask 077; /usr/bin/mkdir -p /home/student/.ssh; "
    "test -f /home/student/.ssh/id_an001_grade || "
    "/usr/bin/ssh-keygen -q -t ed25519 -N '' -f /home/student/.ssh/id_an001_grade"
)
AN001_PUBLIC_KEY_COMMAND = "/usr/bin/cat -- /home/student/.ssh/id_an001_grade.pub"
AN001_PLAYBOOK_COMMAND = (
    "/usr/bin/ansible-playbook -i /tmp/labctl-an001-grade/inventory.ini "
    "/home/student/ansible-lab/site.yml"
)
AN001_CLEANUP_COMMAND = "/usr/bin/rm -rf -- /tmp/labctl-an001-grade"
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureGradeKey student@controller"


@pytest.mark.parametrize(
    ("lab_id", "title"),
    [("LX001", "Users and permissions"), ("AN001", "Ansible controller and web target")],
)
def test_bundled_definition_loads_with_exact_schema(lab_id: str, title: str) -> None:
    definition = load_definition(LABS / lab_id / "lab.yaml")

    assert definition.id == lab_id
    assert definition.title == title
    assert definition.provider == "kvm"
    expected_names = ["node"] if lab_id == "LX001" else ["target", "controller"]
    assert [vm.name for vm in definition.vms] == expected_names
    assert all(vm.ssh_user == "student" for vm in definition.vms)
    assert definition.grading.reset_vms == (() if lab_id == "LX001" else ("target",))
    assert os.access(definition.grader, os.X_OK)
    assert all(os.access(vm.setup, os.X_OK) for vm in definition.vms)


@pytest.mark.parametrize(
    "script", sorted(LABS.rglob("*.sh")), ids=lambda path: str(path.relative_to(LABS))
)
def test_bundled_scripts_do_not_request_conflicting_curl_minimal(script: Path) -> None:
    assert "curl-minimal" not in script.read_text(encoding="utf-8")


def _fake_ssh(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "ssh-argv.jsonl"
    executable = tmp_path / "ssh-fake"
    executable.write_text(
        """#!/usr/bin/python3
import json
import os
import sys

with open(os.environ["FAKE_SSH_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
command = sys.argv[-1]
answers = json.loads(os.environ["FAKE_SSH_ANSWERS"])
answer = answers.get(command)
if answer is None:
    answer = next(
        (
            value
            for key, value in answers.items()
            if key.endswith("*") and command.startswith(key[:-1])
        ),
        None,
    )
if answer is None:
    raise SystemExit(255)
if isinstance(answer, dict):
    print(answer.get("stdout", ""))
    raise SystemExit(answer.get("returncode", 0))
print(answer)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable, log


def _context(tmp_path: Path, lab_id: str) -> tuple[Path, Path, Path]:
    identity = tmp_path / "id_lab"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("fixture, not a key\n", encoding="utf-8")
    known_hosts.write_text(
        "192.0.2.8 ssh-ed25519 controller-key\n192.0.2.9 ssh-ed25519 target-key\n",
        encoding="utf-8",
    )
    names = ("node",) if lab_id == "LX001" else ("controller", "target")
    hosts = {
        name: {
            "address": "192.0.2.8" if name in {"node", "controller"} else "192.0.2.9",
            "ssh_user": "student",
            "private_key_path": str(identity),
            "known_hosts_path": str(known_hosts),
        }
        for name in names
    }
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps({"schema_version": 1, "lab_id": lab_id, "hosts": hosts}),
        encoding="utf-8",
    )
    return context, identity, known_hosts


def _run_grader(
    tmp_path: Path, lab_id: str, answers: dict[str, object]
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    definition = load_definition(LABS / lab_id / "lab.yaml")
    ssh, log = _fake_ssh(tmp_path)
    context, identity, known_hosts = _context(tmp_path, lab_id)
    result = subprocess.run(
        [definition.grader, context],
        env={
            "LABCTL_CONTEXT": str(context),
            "LABCTL_SCHEMA_VERSION": "1",
            "LABCTL_SSH": str(ssh),
            "LABCTL_SSH_IDENTITY": str(identity),
            "LABCTL_SSH_KNOWN_HOSTS": str(known_hosts),
            "FAKE_SSH_LOG": str(log),
            "FAKE_SSH_ANSWERS": json.dumps(answers),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return result, calls


def test_lx001_bundled_grader_still_passes_unchanged(tmp_path: Path) -> None:
    answers = {
        "getent group labops": "labops:x:1001:opsadmin",
        "id -nG opsadmin": "opsadmin labops",
        "stat -c '%U:%G %a' /srv/labshare": "opsadmin:labops 2770",
        "sudo stat -c '%U:%G %a' /srv/labshare/operations.txt": "opsadmin:labops 660",
    }
    result, calls = _run_grader(tmp_path, "LX001", answers)

    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 4
    assert all(line.startswith("PASS LX001: ") for line in result.stdout.splitlines())
    assert all("student@192.0.2.8" in call for call in calls)


def _an001_answers(playbook: object = "playbook succeeded") -> dict[str, object]:
    return {
        AN001_KEY_COMMAND: "",
        AN001_PUBLIC_KEY_COMMAND: PUBLIC_KEY,
        "/usr/bin/python3 -c *": "",
        AN001_PLAYBOOK_COMMAND: playbook,
        AN001_CLEANUP_COMMAND: "",
        "/usr/bin/systemctl is-enabled nginx": "enabled",
        "/usr/bin/systemctl is-active nginx": "active",
        "/usr/bin/stat -c '%U:%G %a' /usr/share/nginx/html/index.html": "root:root 644",
        "/usr/bin/base64 -w0 -- /usr/share/nginx/html/index.html": "TWFuYWdlZCBieSBBbnNpYmxl",
    }


def test_an001_grader_executes_alternate_valid_playbook_then_checks_reset_target(
    tmp_path: Path,
) -> None:
    result, calls = _run_grader(tmp_path, "AN001", _an001_answers())

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 6
    assert all(line.startswith("PASS AN001: ") for line in lines)
    commands = [call[-1] for call in calls]
    assert AN001_PLAYBOOK_COMMAND in commands
    assert AN001_CLEANUP_COMMAND in commands
    assert not any("cat -- /home/student/ansible-lab/site.yml" in command for command in commands)
    assert not any("inventory.ini" in command and command.startswith("cat") for command in commands)
    assert all(str(tmp_path / "id_lab") not in command for command in commands)


@pytest.mark.parametrize(
    "playbook",
    [
        {"returncode": 1, "stdout": "playbook failed"},
        {"returncode": 0, "stdout": "no-op"},
    ],
    ids=["failing", "no-op-final-state-missing"],
)
def test_an001_grader_rejects_failing_or_noop_playbook_on_clean_target(
    tmp_path: Path, playbook: object
) -> None:
    answers = _an001_answers(playbook)
    if playbook == {"returncode": 0, "stdout": "no-op"}:
        answers["/usr/bin/systemctl is-enabled nginx"] = {
            "returncode": 1,
            "stdout": "disabled",
        }
        answers["/usr/bin/systemctl is-active nginx"] = {
            "returncode": 3,
            "stdout": "inactive",
        }
        answers["/usr/bin/stat -c '%U:%G %a' /usr/share/nginx/html/index.html"] = {
            "returncode": 1,
            "stdout": "",
        }
        answers["/usr/bin/base64 -w0 -- /usr/share/nginx/html/index.html"] = {
            "returncode": 1,
            "stdout": "",
        }

    result, calls = _run_grader(tmp_path, "AN001", answers)

    assert result.returncode == 1
    assert "FAIL AN001: controller playbook completed successfully" in result.stdout or any(
        line.startswith("FAIL AN001: nginx") for line in result.stdout.splitlines()
    )
    assert any(call[-1] == AN001_CLEANUP_COMMAND for call in calls)


@pytest.mark.parametrize("stale_kind", ["directory", "file", "symlink"])
def test_an001_controller_staging_replaces_stale_paths_without_following_symlinks(
    tmp_path: Path, stale_kind: str
) -> None:
    grader_path = LABS / "AN001/grade.py"
    specification = importlib.util.spec_from_file_location("an001_grade", grader_path)
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
