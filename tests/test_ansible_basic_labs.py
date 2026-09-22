from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

from labctl.definitions import load_definition

ROOT = Path(__file__).parents[1]
LABS = ROOT / "src/labctl/data/labs"
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureGradeKey student@controller"
AUTHORIZED_KEY_B64 = base64.b64encode((PUBLIC_KEY + "\n").encode()).decode()
WEB_CONTENT_COMMAND = "/usr/bin/base64 -w0 -- /usr/share/nginx/html/index.html"
WEB_CONTENT_B64 = base64.b64encode(b"Reusable Ansible role\n").decode()
ANSIBLE_CONFIG = b"[defaults]\nstdout_callback = default\ncallbacks_enabled =\n"
ANSIBLE_CONFIG_B64 = base64.b64encode(ANSIBLE_CONFIG).decode()


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
state_path = os.environ["FAKE_SSH_STATE"]
try:
    with open(state_path, encoding="utf-8") as stream:
        counts = json.load(stream)
except FileNotFoundError:
    counts = {}
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
if isinstance(answer, list):
    index = counts.get(command, 0)
    answer = answer[min(index, len(answer) - 1)]
    counts[command] = index + 1
    with open(state_path, "w", encoding="utf-8") as stream:
        json.dump(counts, stream)
if isinstance(answer, dict):
    print(answer.get("stdout", ""))
    raise SystemExit(answer.get("returncode", 0))
print(answer)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable, log


def _run_grader(
    tmp_path: Path, lab_id: str, answers: dict[str, object]
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    definition = load_definition(LABS / lab_id / "lab.yaml")
    identity = tmp_path / "id_lab"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("fixture, not a key\n", encoding="utf-8")
    known_hosts.write_text(
        "192.0.2.8 ssh-ed25519 controller-key\n192.0.2.9 ssh-ed25519 target-key\n",
        encoding="utf-8",
    )
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "lab_id": lab_id,
                "hosts": {
                    "controller": {
                        "address": "192.0.2.8",
                        "ssh_user": "student",
                        "private_key_path": str(identity),
                        "known_hosts_path": str(known_hosts),
                    },
                    "target": {
                        "address": "192.0.2.9",
                        "ssh_user": "student",
                        "private_key_path": str(identity),
                        "known_hosts_path": str(known_hosts),
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    ssh, log = _fake_ssh(tmp_path)
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
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return result, calls


def _arrangement_answers(lab_id: str) -> dict[str, object]:
    environment = (
        f"ANSIBLE_CONFIG=/tmp/labctl-{lab_id.lower()}-grade/ansible.cfg "
        "ANSIBLE_NOCOLOR=1 ANSIBLE_STDOUT_CALLBACK=default "
        if lab_id in {"AN101", "AN102"}
        else ""
    )
    name = lab_id.lower()
    key_command = (
        "umask 077; /usr/bin/mkdir -p /home/student/.ssh; "
        f"test -f /home/student/.ssh/id_{name}_grade || "
        f"/usr/bin/ssh-keygen -q -t ed25519 -N '' -f /home/student/.ssh/id_{name}_grade"
    )
    playbook_command = (
        f"{environment}/usr/bin/ansible-playbook "
        f"-i /tmp/labctl-{name}-grade/inventory.ini /home/student/ansible-lab/site.yml"
    )
    return {
        key_command: "",
        f"/usr/bin/cat -- /home/student/.ssh/id_{name}_grade.pub": PUBLIC_KEY,
        "/usr/bin/python3 -c *": "",
        playbook_command: "playbook succeeded",
        f"/usr/bin/rm -rf -- /tmp/labctl-{name}-grade": "",
    }


def test_an002_grades_account_and_shared_directory_outcomes_only(tmp_path: Path) -> None:
    definition = load_definition(LABS / "AN002/lab.yaml")
    assert [vm.name for vm in definition.vms] == ["target", "controller"]
    assert definition.grading.reset_vms == ("target",)
    assert os.access(definition.grader, os.X_OK)
    assert all(os.access(vm.setup, os.X_OK) for vm in definition.vms)

    answers = _arrangement_answers("AN002") | {
        "/usr/bin/cat -- /home/student/ansible-lab/deploy_access_key.pub": PUBLIC_KEY,
        "/usr/bin/getent group automation": "automation:x:2001:deploy,auditor",
        "/usr/bin/id -nG deploy": "deploy automation",
        "/usr/bin/id -nG auditor": "auditor automation",
        "/usr/bin/test -d -- /srv/automation": "",
        "/usr/bin/stat -c '%U:%G %a' /srv/automation": "root:automation 2770",
        "/usr/bin/stat -c '%U:%G %a' /home/deploy/.ssh/authorized_keys": "deploy:deploy 600",
        "/usr/bin/base64 -w0 -- /home/deploy/.ssh/authorized_keys": AUTHORIZED_KEY_B64,
    }
    result, calls = _run_grader(tmp_path, "AN002", answers)

    assert result.returncode == 0, result.stderr
    assert all(line.startswith("PASS AN002: ") for line in result.stdout.splitlines())
    commands = [call[-1] for call in calls]
    assert not any(
        "site.yml" in command and command.startswith("/usr/bin/cat") for command in commands
    )
    assert not any(
        "inventory.ini" in command and command.startswith("/usr/bin/cat") for command in commands
    )
    assert all(str(tmp_path / "id_lab") not in command for command in commands)


def test_an002_rejects_non_directory_with_matching_stat_metadata(tmp_path: Path) -> None:
    answers = _arrangement_answers("AN002") | {
        "/usr/bin/cat -- /home/student/ansible-lab/deploy_access_key.pub": PUBLIC_KEY,
        "/usr/bin/getent group automation": "automation:x:2001:deploy,auditor",
        "/usr/bin/id -nG deploy": "deploy automation",
        "/usr/bin/id -nG auditor": "auditor automation",
        "/usr/bin/test -d -- /srv/automation": {"returncode": 1},
        "/usr/bin/stat -c '%U:%G %a' /srv/automation": "root:automation 2770",
        "/usr/bin/stat -c '%U:%G %a' /home/deploy/.ssh/authorized_keys": "deploy:deploy 600",
        "/usr/bin/base64 -w0 -- /home/deploy/.ssh/authorized_keys": AUTHORIZED_KEY_B64,
    }

    result, _calls = _run_grader(tmp_path, "AN002", answers)

    assert result.returncode == 1
    assert "FAIL AN002: shared path is a directory" in result.stdout


def test_an101_grades_exact_config_service_and_stable_pid_on_second_run(
    tmp_path: Path,
) -> None:
    definition = load_definition(LABS / "AN101/lab.yaml")
    assert [vm.name for vm in definition.vms] == ["target", "controller"]
    assert definition.grading.reset_vms == ("target",)
    assert all(
        os.access(path, os.X_OK)
        for path in [definition.grader, *(vm.setup for vm in definition.vms)]
    )

    playbook = (
        "ANSIBLE_CONFIG=/tmp/labctl-an101-grade/ansible.cfg "
        "ANSIBLE_NOCOLOR=1 ANSIBLE_STDOUT_CALLBACK=default "
        "/usr/bin/ansible-playbook -i /tmp/labctl-an101-grade/inventory.ini "
        "/home/student/ansible-lab/site.yml"
    )
    config = (
        "pool 2.pool.ntp.org iburst\ndriftfile /var/lib/chrony/drift\nmakestep 1.0 3\nrtcsync\n"
    )
    answers = _arrangement_answers("AN101") | {
        playbook: [
            "first run",
            "PLAY RECAP ********\n"
            "target : ok=5 changed=0 unreachable=0 failed=0 "
            "skipped=0 rescued=0 ignored=0",
        ],
        "/usr/bin/systemctl show -p MainPID --value chronyd": ["4242", "4242"],
        "/usr/bin/systemctl is-enabled chronyd": "enabled",
        "/usr/bin/systemctl is-active chronyd": "active",
        "/usr/bin/stat -c '%U:%G %a' /etc/chrony.conf": "root:root 644",
        "/usr/bin/base64 -w0 -- /etc/chrony.conf": base64.b64encode(config.encode()).decode(),
    }
    result, calls = _run_grader(tmp_path, "AN101", answers)

    assert result.returncode == 0, result.stderr
    assert all(line.startswith("PASS AN101: ") for line in result.stdout.splitlines())
    commands = [call[-1] for call in calls]
    assert commands.count(playbook) == 2
    assert any(ANSIBLE_CONFIG_B64 in command for command in commands)
    assert not any(
        "site.yml" in command and command.startswith("/usr/bin/cat") for command in commands
    )
    assert all(str(tmp_path / "id_lab") not in command for command in commands)


def test_an102_grades_web_outcome_and_second_run_recap_not_role_layout(tmp_path: Path) -> None:
    definition = load_definition(LABS / "AN102/lab.yaml")
    assert [vm.name for vm in definition.vms] == ["target", "controller"]
    assert definition.grading.reset_vms == ("target",)
    assert all(
        os.access(path, os.X_OK)
        for path in [definition.grader, *(vm.setup for vm in definition.vms)]
    )

    playbook = (
        "ANSIBLE_CONFIG=/tmp/labctl-an102-grade/ansible.cfg "
        "ANSIBLE_NOCOLOR=1 ANSIBLE_STDOUT_CALLBACK=default /usr/bin/ansible-playbook "
        "-i /tmp/labctl-an102-grade/inventory.ini "
        "/home/student/ansible-lab/site.yml"
    )
    answers = _arrangement_answers("AN102") | {
        playbook: "PLAY RECAP ********\ntarget : ok=5 changed=0 unreachable=0 failed=0",
        "/usr/bin/systemctl is-enabled nginx": "enabled",
        "/usr/bin/systemctl is-active nginx": "active",
        "/usr/bin/stat -c '%U:%G %a' /usr/share/nginx/html/index.html": "root:root 644",
        WEB_CONTENT_COMMAND: WEB_CONTENT_B64,
    }
    result, calls = _run_grader(tmp_path, "AN102", answers)

    assert result.returncode == 0, result.stderr
    assert all(line.startswith("PASS AN102: ") for line in result.stdout.splitlines())
    commands = [call[-1] for call in calls]
    assert commands.count(playbook) == 2
    assert any(ANSIBLE_CONFIG_B64 in command for command in commands)
    assert not any(
        "roles/" in command or ("site.yml" in command and command.startswith("/usr/bin/cat"))
        for command in commands
    )
    assert all(str(tmp_path / "id_lab") not in command for command in commands)


def test_an002_rejects_wrong_authorized_key(tmp_path: Path) -> None:
    answers = _arrangement_answers("AN002") | {
        "/usr/bin/cat -- /home/student/ansible-lab/deploy_access_key.pub": PUBLIC_KEY,
        "/usr/bin/getent group automation": "automation:x:2001:deploy,auditor",
        "/usr/bin/id -nG deploy": "deploy automation",
        "/usr/bin/id -nG auditor": "auditor automation",
        "/usr/bin/stat -c '%U:%G %a' /srv/automation": "root:automation 2770",
        "/usr/bin/stat -c '%U:%G %a' /home/deploy/.ssh/authorized_keys": "deploy:deploy 600",
        "/usr/bin/base64 -w0 -- /home/deploy/.ssh/authorized_keys": "d3Jvbmcta2V5Cg==",
    }

    result, _calls = _run_grader(tmp_path, "AN002", answers)

    assert result.returncode == 1
    assert "FAIL AN002: deploy authorized key is exact" in result.stdout


def test_an101_rejects_restart_on_unchanged_second_run(tmp_path: Path) -> None:
    config = (
        "pool 2.pool.ntp.org iburst\ndriftfile /var/lib/chrony/drift\nmakestep 1.0 3\nrtcsync\n"
    )
    answers = _arrangement_answers("AN101") | {
        "/usr/bin/systemctl show -p MainPID --value chronyd": ["4242", "5252"],
        "/usr/bin/systemctl is-enabled chronyd": "enabled",
        "/usr/bin/systemctl is-active chronyd": "active",
        "/usr/bin/stat -c '%U:%G %a' /etc/chrony.conf": "root:root 644",
        "/usr/bin/base64 -w0 -- /etc/chrony.conf": base64.b64encode(config.encode()).decode(),
    }

    result, _calls = _run_grader(tmp_path, "AN101", answers)

    assert result.returncode == 1
    assert "FAIL AN101: unchanged second run does not restart chronyd" in result.stdout


def test_an101_rejects_changed_recap_despite_stable_pid_and_misleading_output(
    tmp_path: Path,
) -> None:
    config = (
        "pool 2.pool.ntp.org iburst\ndriftfile /var/lib/chrony/drift\nmakestep 1.0 3\nrtcsync\n"
    )
    playbook = (
        "ANSIBLE_CONFIG=/tmp/labctl-an101-grade/ansible.cfg "
        "ANSIBLE_NOCOLOR=1 ANSIBLE_STDOUT_CALLBACK=default "
        "/usr/bin/ansible-playbook -i /tmp/labctl-an101-grade/inventory.ini "
        "/home/student/ansible-lab/site.yml"
    )
    answers = _arrangement_answers("AN101") | {
        playbook: [
            "first run",
            "target : ok=99 changed=0 unreachable=0 failed=0\n"
            "PLAY RECAP ********\n"
            "target : ok=5 changed=1 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0",
        ],
        "/usr/bin/systemctl show -p MainPID --value chronyd": ["4242", "4242"],
        "/usr/bin/systemctl is-enabled chronyd": "enabled",
        "/usr/bin/systemctl is-active chronyd": "active",
        "/usr/bin/stat -c '%U:%G %a' /etc/chrony.conf": "root:root 644",
        "/usr/bin/base64 -w0 -- /etc/chrony.conf": base64.b64encode(config.encode()).decode(),
    }

    result, _calls = _run_grader(tmp_path, "AN101", answers)

    assert result.returncode == 1
    assert "FAIL AN101: second run reports changed=0 for target" in result.stdout


def test_an102_rejects_changed_second_run_even_with_valid_web_outcome(tmp_path: Path) -> None:
    playbook = (
        "ANSIBLE_CONFIG=/tmp/labctl-an102-grade/ansible.cfg "
        "ANSIBLE_NOCOLOR=1 ANSIBLE_STDOUT_CALLBACK=default /usr/bin/ansible-playbook "
        "-i /tmp/labctl-an102-grade/inventory.ini /home/student/ansible-lab/site.yml"
    )
    answers = _arrangement_answers("AN102") | {
        playbook: "PLAY RECAP ********\ntarget : ok=5 changed=1 unreachable=0 failed=0",
        "/usr/bin/systemctl is-enabled nginx": "enabled",
        "/usr/bin/systemctl is-active nginx": "active",
        "/usr/bin/stat -c '%U:%G %a' /usr/share/nginx/html/index.html": "root:root 644",
        WEB_CONTENT_COMMAND: WEB_CONTENT_B64,
    }

    result, _calls = _run_grader(tmp_path, "AN102", answers)

    assert result.returncode == 1
    assert "FAIL AN102: second run reports changed=0 for target" in result.stdout


def test_an101_rejects_failed_or_unreachable_final_recap(tmp_path: Path) -> None:
    config = (
        "pool 2.pool.ntp.org iburst\ndriftfile /var/lib/chrony/drift\nmakestep 1.0 3\nrtcsync\n"
    )
    playbook = (
        "ANSIBLE_CONFIG=/tmp/labctl-an101-grade/ansible.cfg "
        "ANSIBLE_NOCOLOR=1 ANSIBLE_STDOUT_CALLBACK=default "
        "/usr/bin/ansible-playbook -i /tmp/labctl-an101-grade/inventory.ini "
        "/home/student/ansible-lab/site.yml"
    )
    answers = _arrangement_answers("AN101") | {
        playbook: [
            "first run",
            "PLAY RECAP ********\n"
            "target : ok=5 changed=0 unreachable=1 failed=1 skipped=0 rescued=0 ignored=0",
        ],
        "/usr/bin/systemctl show -p MainPID --value chronyd": ["4242", "4242"],
        "/usr/bin/systemctl is-enabled chronyd": "enabled",
        "/usr/bin/systemctl is-active chronyd": "active",
        "/usr/bin/stat -c '%U:%G %a' /etc/chrony.conf": "root:root 644",
        "/usr/bin/base64 -w0 -- /etc/chrony.conf": base64.b64encode(config.encode()).decode(),
    }

    result, _calls = _run_grader(tmp_path, "AN101", answers)

    assert result.returncode == 1
    assert "FAIL AN101: second run reports changed=0 for target" in result.stdout


def test_an102_rejects_stale_clean_recap_before_changed_final_recap(tmp_path: Path) -> None:
    playbook = (
        "ANSIBLE_CONFIG=/tmp/labctl-an102-grade/ansible.cfg "
        "ANSIBLE_NOCOLOR=1 ANSIBLE_STDOUT_CALLBACK=default /usr/bin/ansible-playbook "
        "-i /tmp/labctl-an102-grade/inventory.ini /home/student/ansible-lab/site.yml"
    )
    answers = _arrangement_answers("AN102") | {
        playbook: (
            "PLAY RECAP ********\n"
            "target : ok=5 changed=0 unreachable=0 failed=0\n"
            "PLAY RECAP ********\n"
            "target : ok=5 changed=1 unreachable=0 failed=0"
        ),
        "/usr/bin/systemctl is-enabled nginx": "enabled",
        "/usr/bin/systemctl is-active nginx": "active",
        "/usr/bin/stat -c '%U:%G %a' /usr/share/nginx/html/index.html": "root:root 644",
        WEB_CONTENT_COMMAND: WEB_CONTENT_B64,
    }

    result, _calls = _run_grader(tmp_path, "AN102", answers)

    assert result.returncode == 1
    assert "FAIL AN102: second run reports changed=0 for target" in result.stdout
