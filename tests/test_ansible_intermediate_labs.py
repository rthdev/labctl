from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

from labctl.definitions import load_definition

ROOT = Path(__file__).parents[1]
LABS = ROOT / "src/labctl/data/labs"


def test_an201_declares_separate_reset_app_proxy_and_persistent_controller() -> None:
    definition = load_definition(LABS / "AN201/lab.yaml")

    assert definition.id == "AN201"
    assert [vm.name for vm in definition.vms] == ["app", "proxy", "controller"]
    assert definition.grading.reset_vms == ("app", "proxy")
    assert definition.vms[-1].depends_on == ("app", "proxy")
    assert all(vm.ssh_user == "student" for vm in definition.vms)
    assert os.access(definition.grader, os.X_OK)
    assert all(os.access(vm.setup, os.X_OK) for vm in definition.vms)


def _run_grader(
    tmp_path: Path,
    lab_id: str,
    names: tuple[str, ...],
    answers: dict[str, object],
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    identity = tmp_path / "management-key"
    identity.write_text("management key fixture", encoding="utf-8")
    addresses = {name: f"192.0.2.{index + 10}" for index, name in enumerate(names)}
    known_hosts_paths: dict[str, Path] = {}
    for name, address in addresses.items():
        known_hosts = tmp_path / f"known_hosts-{name}"
        known_hosts.write_text(f"{address} ssh-ed25519 fixture-{name}\n", encoding="utf-8")
        known_hosts_paths[name] = known_hosts
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "lab_id": lab_id,
                "hosts": {
                    name: {
                        "address": address,
                        "ssh_user": "student",
                        "private_key_path": str(identity),
                        "known_hosts_path": str(known_hosts_paths[name]),
                    }
                    for name, address in addresses.items()
                },
            }
        ),
        encoding="utf-8",
    )
    log = tmp_path / "ssh.jsonl"
    fake = tmp_path / "ssh"
    fake.write_text(
        """#!/usr/bin/python3
import json, os, sys
with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
destination, command = sys.argv[-2:]
answers = json.loads(os.environ['FAKE_ANSWERS'])
answer = answers.get(destination + '|' + command, answers.get(command))
if answer is None:
    answer = next(
        (v for k, v in answers.items() if k.endswith('*') and command.startswith(k[:-1])),
        None,
    )
if answer is None:
    raise SystemExit(255)
if isinstance(answer, dict):
    print(answer.get('stdout', ''))
    raise SystemExit(answer.get('returncode', 0))
print(answer)
""",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    result = subprocess.run(
        [LABS / lab_id / "grade.py", context],
        env={
            **os.environ,
            "LABCTL_SSH": str(fake),
            "FAKE_LOG": str(log),
            "FAKE_ANSWERS": json.dumps(answers),
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


def _assert_only_target_host_keys_staged(calls: list[list[str]], targets: tuple[str, ...]) -> None:
    stage = next(
        call[-1]
        for call in calls
        if call[-1].startswith("/usr/bin/python3 -c ") and "inventory.ini" in call[-1]
    )
    target_entries = b"".join(
        f"192.0.2.{index + 10} ssh-ed25519 fixture-{name}\n".encode()
        for index, name in enumerate(targets)
    )
    assert base64.b64encode(target_entries).decode() in stage
    controller_entry = f"192.0.2.{len(targets) + 10} ssh-ed25519 fixture-controller\n".encode()
    assert base64.b64encode(controller_entry).decode() not in stage


def test_an201_runs_controller_playbook_and_grades_both_tiers_end_to_end(tmp_path: Path) -> None:
    answers: dict[str, object] = {
        "umask 077; *": "",
        "/usr/bin/cat -- /home/student/.ssh/id_an201_grade.pub": (
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureGradeKey controller"
        ),
        "/usr/bin/python3 -c *": "",
        "/usr/bin/ansible-playbook -i /tmp/labctl-an201-grade/inventory.ini "
        "/home/student/ansible-lab/site.yml": "playbook succeeded",
        "/usr/bin/rm -rf -- /tmp/labctl-an201-grade": "",
        "/usr/bin/systemctl is-enabled nginx": "enabled",
        "/usr/bin/systemctl is-active nginx": "active",
        "/usr/bin/curl -fsS --max-time 5 http://127.0.0.1/": "AN201 application",
        "/usr/bin/sudo -n /usr/bin/systemctl stop nginx": "",
        "/usr/bin/sudo -n /usr/bin/systemctl start nginx": "",
        "/usr/bin/curl -fsS --max-time 5 -H 'X-Labctl-Probe: backend-stopped' http://127.0.0.1/": {
            "returncode": 7,
            "stdout": "",
        },
    }

    result, calls = _run_grader(tmp_path, "AN201", ("app", "proxy", "controller"), answers)

    assert result.returncode == 0, result.stderr
    assert all(line.startswith("PASS AN201: ") for line in result.stdout.splitlines())
    commands = [call[-1] for call in calls]
    destinations = [call[-2] for call in calls]
    assert any(
        "controller@" not in destination and command.startswith("/usr/bin/python3 -c ")
        for destination, command in zip(destinations, commands, strict=True)
    )
    assert commands.count("/usr/bin/systemctl is-active nginx") == 2
    assert commands.count("/usr/bin/curl -fsS --max-time 5 http://127.0.0.1/") == 2
    assert "/usr/bin/sudo -n /usr/bin/systemctl stop nginx" in commands
    assert "/usr/bin/sudo -n /usr/bin/systemctl start nginx" in commands
    assert any("X-Labctl-Probe: backend-stopped" in command for command in commands)
    assert not any(
        "site.yml" in command and ("cat" in command or "base64" in command) for command in commands
    )
    assert not any(str(tmp_path / "management-key") in command for command in commands)
    _assert_only_target_host_keys_staged(calls, ("app", "proxy"))
    assert commands[-1] == "/usr/bin/rm -rf -- /tmp/labctl-an201-grade"


def test_an202_grades_persistent_loop_storage_web_firewall_and_selinux(tmp_path: Path) -> None:
    definition = load_definition(LABS / "AN202/lab.yaml")
    assert [vm.name for vm in definition.vms] == ["target", "controller"]
    assert definition.grading.reset_vms == ("target",)
    answers: dict[str, object] = {
        "umask 077; *": "",
        "/usr/bin/cat -- /home/student/.ssh/id_an202_grade.pub": (
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureGradeKey controller"
        ),
        "/usr/bin/python3 -c *": "",
        "/usr/bin/ansible-playbook -i /tmp/labctl-an202-grade/inventory.ini "
        "/home/student/ansible-lab/site.yml": "playbook succeeded",
        "/usr/bin/rm -rf -- /tmp/labctl-an202-grade": "",
        "/usr/bin/stat -c '%s %b' -- /var/lib/an202/storage.img": "1073741824 8",
        "/usr/bin/mountpoint -q /srv/an202": "",
        "/usr/bin/findmnt -n -o SOURCE,FSTYPE --target /srv/an202": "/dev/loop7 ext4",
        "/usr/sbin/losetup --list --noheadings --output NAME,BACK-FILE": (
            "/dev/loop7 /var/lib/an202/storage.img"
        ),
        "/usr/bin/cat -- /etc/fstab": (
            "/var/lib/an202/storage.img /srv/an202 ext4 defaults,nodev,loop,nosuid 0 0"
        ),
        "/usr/bin/systemctl is-enabled httpd": "enabled",
        "/usr/bin/systemctl is-active httpd": "active",
        "/usr/bin/cat -- /srv/an202/www/index.html": "AN202 durable web",
        "/usr/bin/sudo -n /usr/bin/ln -- /srv/an202/www/index.html "
        "/srv/an202/www/labctl-an202-probe-*": "",
        "/usr/bin/curl -fsS --max-time 5 "
        "http://192.0.2.10/labctl-an202-probe-*": "AN202 durable web",
        "/usr/bin/sudo -n /usr/bin/rm -f -- /srv/an202/www/labctl-an202-probe-*": "",
        "/usr/bin/firewall-cmd --quiet --query-service=http": "",
        "/usr/bin/firewall-cmd --quiet --permanent --query-service=http": "",
        "/usr/bin/systemctl is-enabled firewalld": "enabled",
        "/usr/bin/systemctl is-active firewalld": "active",
        "/usr/sbin/getenforce": "Enforcing",
        "/usr/sbin/matchpathcon -V /srv/an202/www/index.html": "verified",
    }
    result, calls = _run_grader(tmp_path, "AN202", ("target", "controller"), answers)

    assert result.returncode == 0, result.stderr
    assert all(line.startswith("PASS AN202: ") for line in result.stdout.splitlines())
    commands = [call[-1] for call in calls]
    destinations = [call[-2] for call in calls]
    assert not any(
        "setenforce 0" in command or ("site.yml" in command and "cat" in command)
        for command in commands
    )
    assert not any("http://127.0.0.1/" in command for command in commands)
    assert any(
        destination == "student@192.0.2.11"
        and command.startswith(
            "/usr/bin/curl -fsS --max-time 5 http://192.0.2.10/labctl-an202-probe-"
        )
        for destination, command in zip(destinations, commands, strict=True)
    )
    assert any(
        destination == "student@192.0.2.10"
        and command == "/usr/bin/cat -- /srv/an202/www/index.html"
        for destination, command in zip(destinations, commands, strict=True)
    )
    assert "/usr/bin/stat -c '%s %b' -- /var/lib/an202/storage.img" in commands
    assert "/usr/bin/findmnt -n -o SOURCE,FSTYPE --target /srv/an202" in commands
    assert "/usr/sbin/losetup --list --noheadings --output NAME,BACK-FILE" in commands
    assert "/usr/bin/firewall-cmd --quiet --query-service=http" in commands
    assert "/usr/bin/firewall-cmd --quiet --permanent --query-service=http" in commands
    assert "/usr/bin/systemctl is-enabled firewalld" in commands
    assert "/usr/bin/systemctl is-active firewalld" in commands
    assert not any(str(tmp_path / "management-key") in command for command in commands)
    _assert_only_target_host_keys_staged(calls, ("target",))
    assert commands[-1] == "/usr/bin/rm -rf -- /tmp/labctl-an202-grade"


def test_an301_grades_three_hosts_and_requires_an_idempotent_second_run(tmp_path: Path) -> None:
    definition = load_definition(LABS / "AN301/lab.yaml")
    assert [vm.name for vm in definition.vms] == ["web1", "web2", "web3", "controller"]
    assert definition.grading.reset_vms == ("web1", "web2", "web3")
    playbook = (
        "/usr/bin/env ANSIBLE_CONFIG=/tmp/labctl-an301-grade/ansible.cfg "
        "ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0 ANSIBLE_STDOUT_CALLBACK=default "
        "ANSIBLE_CALLBACKS_ENABLED= "
        "/usr/bin/ansible-playbook -i /tmp/labctl-an301-grade/inventory.ini "
        "/home/student/ansible-lab/site.yml"
    )
    answers: dict[str, object] = {
        "umask 077; *": "",
        "/usr/bin/cat -- /home/student/.ssh/id_an301_grade.pub": (
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureGradeKey controller"
        ),
        "/usr/bin/python3 -c *": "",
        playbook: (
            "PLAY RECAP ****\n"
            "web1 : ok=8 changed=0 unreachable=0 failed=0\n"
            "web2 : ok=8 changed=0 unreachable=0 failed=0\n"
            "web3 : ok=8 changed=0 unreachable=0 failed=0"
        ),
        "/usr/bin/rm -rf -- /tmp/labctl-an301-grade": "",
        "/usr/bin/systemctl is-enabled nginx": "enabled",
        "/usr/bin/systemctl is-active nginx": "active",
        "/usr/bin/curl -fsS --max-time 5 http://127.0.0.1/": "AN301 version 3.1.0",
    }
    result, calls = _run_grader(tmp_path, "AN301", ("web1", "web2", "web3", "controller"), answers)

    assert result.returncode == 0, result.stderr
    assert "PASS AN301: second playbook run changed zero hosts" in result.stdout
    commands = [call[-1] for call in calls]
    assert commands.count(playbook) == 2
    assert commands.count("/usr/bin/curl -fsS --max-time 5 http://127.0.0.1/") == 3
    assert not any(str(tmp_path / "management-key") in command for command in commands)
    stage = next(
        command for command in commands if "inventory.ini" in command and "python3 -c" in command
    )
    config = b"[defaults]\nstdout_callback=default\nforce_color=False\ncallbacks_enabled=\n"
    assert base64.b64encode(config).decode() in stage
    _assert_only_target_host_keys_staged(calls, ("web1", "web2", "web3"))
    assert commands[-1] == "/usr/bin/rm -rf -- /tmp/labctl-an301-grade"


def test_an302_grades_accounts_secret_and_narrow_privilege_without_exposing_secret(
    tmp_path: Path,
) -> None:
    definition = load_definition(LABS / "AN302/lab.yaml")
    assert [vm.name for vm in definition.vms] == ["target", "controller"]
    assert definition.grading.reset_vms == ("target",)
    answers: dict[str, object] = {
        "umask 077; *": "",
        "/usr/bin/cat -- /home/student/.ssh/id_an302_grade.pub": (
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureGradeKey controller"
        ),
        "/usr/bin/python3 -c *": "",
        "/usr/bin/ansible-playbook -i /tmp/labctl-an302-grade/inventory.ini "
        "/home/student/ansible-lab/site.yml": "playbook succeeded",
        "/usr/bin/rm -rf -- /tmp/labctl-an302-grade": "",
        "/usr/bin/id -u deployer": "1001",
        "/usr/bin/id -u auditor": "1002",
        "/usr/bin/id -nG deployer": "deployer",
        "/usr/bin/id -nG auditor": "auditor",
        "/usr/bin/sudo -n /usr/bin/stat -c '%U:%G %a' /etc/an302/secret": ("root:auditor 640"),
        "/usr/bin/sudo -n -u auditor /usr/bin/sha256sum -- /etc/an302/secret": (
            "209cfacd9160c840c8b0f05dd402020c4073a1d6bb2627cc247238328610995f  /etc/an302/secret"
        ),
        "/usr/bin/sudo -n /usr/bin/stat -c '%U:%G %a' /etc/sudoers.d/an302-deployer": (
            "root:root 440"
        ),
        "/usr/bin/sudo -n /usr/bin/base64 -w0 -- /etc/sudoers.d/an302-deployer": (
            "ZGVwbG95ZXIgQUxMPShyb290KSAvdXNyL2Jpbi9zeXN0ZW1jdGwgcmVzdGFydCBhbjMwMi1hZ2VudAo="
        ),
        "/usr/bin/sudo -n -u deployer /usr/bin/cat /etc/an302/secret": {
            "returncode": 1,
            "stdout": "",
        },
    }
    result, calls = _run_grader(tmp_path, "AN302", ("target", "controller"), answers)

    assert result.returncode == 0, result.stderr
    assert all(line.startswith("PASS AN302: ") for line in result.stdout.splitlines())
    commands = [call[-1] for call in calls]
    combined_output = result.stdout + result.stderr
    assert "AN302-rotation-key" not in combined_output
    assert not any("AN302-rotation-key" in command for command in commands)
    assert not any(str(tmp_path / "management-key") in command for command in commands)
    assert "/usr/bin/sudo -n -u auditor /usr/bin/sha256sum -- /etc/an302/secret" in commands
    assert "/usr/bin/sudo -n /usr/bin/base64 -w0 -- /etc/sudoers.d/an302-deployer" in commands
    assert "/usr/bin/sudo -n -u deployer /usr/bin/cat /etc/an302/secret" in commands
    _assert_only_target_host_keys_staged(calls, ("target",))
    assert commands[-1] == "/usr/bin/rm -rf -- /tmp/labctl-an302-grade"
