# ruff: noqa: E501, S108
"""Bundled progressive Linux lab catalog tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from labctl.definitions import load_definition

ROOT = Path(__file__).parents[1]
LABS = ROOT / "src/labctl/data/labs"


def _fake_ssh(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "ssh.jsonl"
    ssh = tmp_path / "ssh"
    ssh.write_text(
        """#!/usr/bin/python3
import json, os, sys
with open(os.environ['FAKE_SSH_LOG'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
answer = json.loads(os.environ['FAKE_SSH_ANSWERS']).get(sys.argv[-1])
if answer is None:
    raise SystemExit(255)
if isinstance(answer, dict):
    print(answer.get('stdout', ''))
    raise SystemExit(answer.get('returncode', 0))
print(answer)
""",
        encoding="utf-8",
    )
    ssh.chmod(0o700)
    return ssh, log


def _run(
    tmp_path: Path, lab_id: str, answers: dict[str, object]
) -> subprocess.CompletedProcess[str]:
    definition = load_definition(LABS / lab_id / "lab.yaml")
    tmp_path.mkdir(parents=True, exist_ok=True)
    identity = tmp_path / "id"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("fixture\n", encoding="utf-8")
    known_hosts.write_text("192.0.2.10 ssh-ed25519 fixture\n", encoding="utf-8")
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "lab_id": lab_id,
                "hosts": {
                    "node": {
                        "address": "192.0.2.10",
                        "ssh_user": "student",
                        "private_key_path": str(identity),
                        "known_hosts_path": str(known_hosts),
                    }
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
            "FAKE_SSH_ANSWERS": json.dumps(answers),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls
    assert all("StrictHostKeyChecking=yes" in call for call in calls)
    assert all(f"UserKnownHostsFile={known_hosts}" in call for call in calls)
    assert all(str(identity) in call for call in calls)
    return result


def _assert_lab(tmp_path: Path, lab_id: str, title: str, answers: dict[str, object]) -> None:
    definition = load_definition(LABS / lab_id / "lab.yaml")
    assert definition.id == lab_id
    assert definition.title == title
    assert definition.provider == "kvm"
    assert [vm.name for vm in definition.vms] == ["node"]
    vm = definition.vms[0]
    assert vm.image == "rocky:9"
    assert vm.ssh_user == "student"
    assert vm.ram_bytes >= 1024**3
    assert os.access(vm.setup, os.X_OK)
    assert os.access(definition.grader, os.X_OK)
    assert len(definition.goal.split()) >= 4
    assert len(definition.instructions.splitlines()) >= 4
    setup = vm.setup.read_text(encoding="utf-8")
    assert "/home/student/LAB.md" in setup
    passed = _run(tmp_path, lab_id, answers)
    assert passed.returncode == 0, passed.stderr
    lines = passed.stdout.splitlines()
    assert lines and all(line.startswith(f"PASS {lab_id}: ") for line in lines)
    for command, answer in answers.items():
        if isinstance(answer, dict) and answer.get("returncode", 0) != 0:
            wrong_returncode_answers = dict(answers)
            wrong_returncode_answers[command] = {**answer, "returncode": 0}
            wrong_returncode = _run(
                tmp_path / f"wrong-returncode-{len(command)}",
                lab_id,
                wrong_returncode_answers,
            )
            assert wrong_returncode.returncode == 1
    failed_answers = dict(answers)
    first = next(iter(failed_answers))
    failed_answers[first] = {"returncode": 1, "stdout": ""}
    failed = _run(tmp_path / "failure", lab_id, failed_answers)
    assert failed.returncode == 1
    assert f"FAIL {lab_id}: " in failed.stdout


def test_lx002_files_links_and_text_processing(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "LX002",
        "Files, links, and text processing",
        {
            "/usr/bin/test -d /srv/lx002/archive -a -f /srv/lx002/source/records.txt": "",
            "/usr/bin/test /srv/lx002/source/records.txt -ef /srv/lx002/archive/records.hard": "",
            "/usr/bin/readlink -f /home/student/current-records": "/srv/lx002/source/records.txt",
            "/usr/bin/cat /srv/lx002/report.txt": "ERROR 2\nINFO 2\nWARN 2",
            "/usr/bin/stat -c '%U:%G %a' /srv/lx002/report.txt": "student:student 640",
        },
    )


def test_lx101_packages_services_and_logs(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "LX101",
        "Packages, services, and logs",
        {
            """/usr/bin/bash -c 'rpm -q httpd >/dev/null && echo installed'""": "installed",
            "/usr/bin/systemctl is-enabled httpd": "enabled",
            "/usr/bin/systemctl is-active httpd": "active",
            "/usr/bin/curl -fsS http://127.0.0.1/health": "LX101 healthy",
            """/usr/bin/sudo /usr/bin/bash -c 'systemd-analyze cat-config systemd/journald.conf | awk -F= "/^[[:space:]]*Storage[[:space:]]*=/{value=\\$2} END {gsub(/[[:space:]]/, "", value); if (value == "persistent") print value}"'""": "persistent",
            """/usr/bin/sudo /usr/bin/bash -c 'test -d /var/log/journal && journalctl --directory=/var/log/journal --quiet -u httpd -n 1 | grep -q . && echo operating'""": "operating",
        },
    )


def test_lx102_persistent_storage_mounts(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "LX102",
        "Persistent storage and mounts",
        {
            """/usr/bin/sudo /usr/bin/bash -c 'source=$(findmnt -rn -o SOURCE --target /srv/archive); image_loop=$(losetup -j /var/lib/labctl-media/lx102-storage.img | head -n 1 | cut -d: -f1); test "$(findmnt -rn -o FSTYPE --target /srv/archive)" = xfs && test -n "$image_loop" && test "$source" = "$image_loop" && echo lab-medium'""": "lab-medium",
            """/usr/bin/bash -c 'findmnt --verify --tab-file /etc/fstab >/dev/null && echo valid'""": "valid",
            "/usr/bin/grep -F '/var/lib/labctl-media/lx102-storage.img /srv/archive xfs' /etc/fstab": "/var/lib/labctl-media/lx102-storage.img /srv/archive xfs loop,nofail 0 0",
            "/usr/bin/cat /srv/archive/persistent.txt": "LX102 persistent storage",
        },
    )


def test_lx201_network_and_firewall(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "LX201",
        "Network and firewall operations",
        {
            "/usr/bin/systemctl is-active NetworkManager": "active",
            """/usr/bin/sudo /usr/bin/bash -c 'read -r device < /var/lib/labctl/lx201-network-baseline; uuid=$(sed -n 2p /var/lib/labctl/lx201-network-baseline); address=$(sed -n 3p /var/lib/labctl/lx201-network-baseline); connection=$(nmcli -g GENERAL.CONNECTION device show "$device") && test "$(nmcli -g connection.uuid connection show "$connection")" = "$uuid" && test "$(nmcli -g IP4.ADDRESS device show "$device" | head -n 1)" = "$address" && echo preserved'""": "preserved",
            "/usr/bin/systemctl is-enabled firewalld": "enabled",
            "/usr/bin/systemctl is-active firewalld": "active",
            """/usr/bin/sudo /usr/bin/bash -c 'firewall-cmd --reload >/dev/null && echo reloaded'""": "reloaded",
            "/usr/bin/sudo /usr/bin/firewall-cmd --permanent --zone=public --query-service=http": "yes",
            "/usr/bin/sudo /usr/bin/firewall-cmd --permanent --zone=public --query-service=cockpit": {
                "stdout": "no",
                "returncode": 1,
            },
            "/usr/bin/sudo /usr/bin/firewall-cmd --zone=public --query-service=http": "yes",
            "/usr/bin/sudo /usr/bin/firewall-cmd --zone=public --query-service=cockpit": {
                "stdout": "no",
                "returncode": 1,
            },
            "/usr/bin/curl -fsS http://127.0.0.1/network-health": "LX201 reachable",
        },
    )


def test_lx202_scheduled_maintenance(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "LX202",
        "Scheduled jobs and system maintenance",
        {
            "/usr/bin/test -x /usr/local/sbin/lx202-maintenance": "",
            """/usr/bin/bash -c 'systemctl cat lx202-maintenance.service | grep -Fx Type=oneshot >/dev/null && systemctl cat lx202-maintenance.service | grep -Fx ExecStart=/usr/local/sbin/lx202-maintenance >/dev/null && echo valid'""": "valid",
            "/usr/bin/systemctl is-enabled lx202-maintenance.timer": "enabled",
            "/usr/bin/systemctl is-active lx202-maintenance.timer": "active",
            "/usr/bin/systemctl show lx202-maintenance.timer -p Persistent --value": "yes",
            "/usr/bin/systemctl show lx202-maintenance.timer -p Unit --value": "lx202-maintenance.service",
            """/usr/bin/bash -c 'systemctl cat lx202-maintenance.timer | grep -Eq "^OnCalendar=(\\*-[*]-[*] )?02:15(:00)?$" && echo scheduled'""": "scheduled",
            """/usr/bin/sudo /usr/bin/bash -c 'install -d -m 0755 /var/tmp/lx202-cache; printf stale > /var/tmp/lx202-cache/stale.tmp; rm -f /var/log/lx202-maintenance.log; systemctl start lx202-maintenance.service && echo invoked'""": "invoked",
            "/usr/bin/test ! -e /var/tmp/lx202-cache/stale.tmp": "",
            "/usr/bin/cat /var/log/lx202-maintenance.log": "maintenance complete",
        },
    )


def test_lx301_secure_web_service_selinux(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "LX301",
        "Secure web service with SELinux",
        {
            "/usr/sbin/getenforce": "Enforcing",
            """/usr/bin/bash -c 'grep -Eq "^[[:space:]]*SELINUX=enforcing[[:space:]]*$" /etc/selinux/config && echo enforcing'""": "enforcing",
            """/usr/bin/sudo /usr/bin/bash -c '! semanage permissive -l | grep -Fxq httpd_t && echo confined'""": "confined",
            "/usr/bin/systemctl is-enabled httpd": "enabled",
            "/usr/bin/systemctl is-active httpd": "active",
            """/usr/bin/sudo /usr/bin/bash -c 'semanage port -l | grep -E "^http_port_t[[:space:]]+tcp.*(^|[,[:space:]])8088($|[,[:space:]])" >/dev/null && echo labeled'""": "labeled",
            """/usr/bin/sudo /usr/bin/bash -c 'actual=$(stat -c %C /srv/secureweb/index.html); expected=$(matchpathcon -n /srv/secureweb/index.html); test "$actual" = "$expected" && printf %s "$actual" | grep -q ":httpd_sys_content_t:" && echo labeled'""": "labeled",
            "/usr/bin/curl -fsS http://127.0.0.1:8088/": "LX301 secure service",
        },
    )


def test_lx302_lvm_expansion_recovery(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "LX302",
        "LVM expansion and recovery",
        {
            """/usr/bin/sudo /usr/bin/bash -c 'pv1=$(losetup -j /var/lib/labctl-media/lx302-pv1.img | head -n 1 | cut -d: -f1); pv2=$(losetup -j /var/lib/labctl-media/lx302-pv2.img | head -n 1 | cut -d: -f1); test -n "$pv1" && test -n "$pv2" && test "$pv1" != "$pv2" && test "$(pvs --noheadings -o vg_name "$pv1" | tr -d " ")" = vgdata && test "$(pvs --noheadings -o vg_name "$pv2" | tr -d " ")" = vgdata && echo supplied'""": "supplied",
            """/usr/bin/sudo /usr/bin/bash -c 'test "$(vgs --noheadings -o pv_count --nosuffix vgdata | tr -d " ")" -ge 2 && echo expanded'""": "expanded",
            """/usr/bin/sudo /usr/bin/bash -c 'test "$(lvs --noheadings --units m --nosuffix -o lv_size vgdata/lvdata | cut -d. -f1 | tr -d " ")" -ge 700 && echo large-enough'""": "large-enough",
            """/usr/bin/sudo /usr/bin/bash -c 'test "$(cat /proc/sys/kernel/random/boot_id)" != "$(cat /var/lib/labctl/lx302-boot-baseline)" && echo rebooted'""": "rebooted",
            "/usr/bin/systemctl is-enabled lx302-loop-pvs.service": "enabled",
            "/usr/bin/systemctl is-active lx302-loop-pvs.service": "active",
            """/usr/bin/sudo /usr/bin/bash -c 'unit=$(systemctl cat lx302-loop-pvs.service); printf %s "$unit" | grep -Eq "^DefaultDependencies=no$" && printf %s "$unit" | grep -Eq "^Before=.*local-fs-pre.target" && printf %s "$unit" | grep -Eq "^Before=.*lvm2-monitor.service" && printf %s "$unit" | grep -Eq "^ExecStart=.*/losetup .*lx302-pv1.img" && printf %s "$unit" | grep -Eq "^ExecStart=.*/losetup .*lx302-pv2.img" && printf %s "$unit" | grep -Eq "^ExecStart=.*/pvscan([[:space:]]|$)" && printf %s "$unit" | grep -Eq "^WantedBy=sysinit.target$" && systemd-analyze verify lx302-loop-pvs.service >/dev/null 2>&1 && echo ordered'""": "ordered",
            """/usr/bin/bash -c 'findmnt --verify --tab-file /etc/fstab >/dev/null && echo valid'""": "valid",
            """/usr/bin/bash -c 'grep -Eq "^[^#]*(/dev/vgdata/lvdata|/dev/mapper/vgdata-lvdata)[[:space:]]+/srv/lvmdata[[:space:]]+xfs([[:space:]]|$)" /etc/fstab && echo persistent'""": "persistent",
            """/usr/bin/bash -c 'set -- $(findmnt -rn -M /srv/lvmdata -o TARGET,SOURCE,FSTYPE); test "$1" = /srv/lvmdata && { test "$2" = /dev/mapper/vgdata-lvdata || test "$2" = /dev/vgdata/lvdata; } && test "$3" = xfs && echo exact'""": "exact",
            """/usr/bin/sudo /usr/bin/bash -c 'xfs_info /srv/lvmdata >/dev/null && test "$(df -m --output=size /srv/lvmdata | tail -1)" -ge 650 && echo grown'""": "grown",
            "/usr/bin/cat /srv/lvmdata/recovery.txt": "LX302 recovered and expanded",
        },
    )


def test_lx401_boot_and_root_password_recovery(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "LX401",
        "Boot failure and root-password recovery",
        {
            """/usr/bin/sudo /usr/bin/bash -c 'hash=$(getent shadow root | cut -d: -f2); test -n "$hash" && test "$hash" != "$(cat /var/lib/labctl/lx401-root-baseline)" && case "$hash" in \\!*|\\**) false;; esac && passwd -S root | grep -q " PS " && echo recovered'""": "recovered",
            """/usr/bin/sudo /usr/bin/bash -c 'test "$(cat /proc/sys/kernel/random/boot_id)" != "$(cat /var/lib/labctl/lx401-boot-baseline)" && echo rebooted'""": "rebooted",
            "/usr/sbin/getenforce": "Enforcing",
            """/usr/bin/bash -c 'grep -Eq "^[[:space:]]*SELINUX=enforcing[[:space:]]*$" /etc/selinux/config && echo enforcing'""": "enforcing",
            """/usr/bin/sudo /usr/bin/bash -c 'grep -Eq "(^|[[:space:]\\"])audit=0([[:space:]\\"]|$)" /etc/default/grub && ! grep -Eq "audit=O|selinux=0|enforcing=0" /etc/default/grub && echo corrected'""": "corrected",
            """/usr/bin/sudo /usr/bin/bash -c 'for entry in /boot/loader/entries/*.conf; do grep -Eq "^options .*([[:space:]])audit=0([[:space:]]|$)" "$entry" && ! grep -Eq "audit=O|selinux=0|enforcing=0" "$entry" || exit 1; done; echo corrected'""": "corrected",
            """/usr/bin/bash -c 'grep -Eq "(^|[[:space:]])audit=0([[:space:]]|$)" /proc/cmdline && ! grep -Eq "audit=O|selinux=0|enforcing=0" /proc/cmdline && echo running-correct'""": "running-correct",
            """/usr/bin/bash -c 'findmnt --verify --tab-file /etc/fstab >/dev/null && echo valid'""": "valid",
            "/usr/bin/grep -F '/var/lib/labctl-media/lx401-recovery.img /srv/recovery xfs' /etc/fstab": "/var/lib/labctl-media/lx401-recovery.img /srv/recovery xfs loop,nofail 0 0",
            """/usr/bin/sudo /usr/bin/bash -c 'source=$(findmnt -rn -o SOURCE --target /srv/recovery); image_loop=$(losetup -j /var/lib/labctl-media/lx401-recovery.img | head -n 1 | cut -d: -f1); test -n "$image_loop" && test "$source" = "$image_loop" && echo mounted'""": "mounted",
            "/usr/bin/systemctl is-system-running": "running",
            "/usr/bin/systemctl get-default": "multi-user.target",
            """/usr/bin/sudo /usr/bin/bash -c 'actual=$(stat -c %C /etc/shadow); expected=$(matchpathcon -n /etc/shadow); test "$actual" = "$expected" && test -z "$(restorecon -n -v /etc/shadow)" && echo labels-clean'""": "labels-clean",
            "/usr/bin/test ! -e /.autorelabel": "",
        },
    )


def test_lx402_production_incident_diagnosis(tmp_path: Path) -> None:
    setup = (LABS / "LX402/setup.sh").read_text(encoding="utf-8")
    assert "statvfs" in setup
    assert "CPUQuota=20%" in setup
    _assert_lab(
        tmp_path,
        "LX402",
        "Production incident diagnosis",
        {
            """/usr/bin/sudo /usr/bin/bash -c 'set -- $(findmnt -rn -o FSTYPE,SOURCE --target /srv/incident); image_loop=$(losetup -j /var/lib/labctl-media/lx402-incident.img | head -n 1 | cut -d: -f1); test "$1" = ext4 && test -n "$image_loop" && test "$2" = "$image_loop" && echo isolated'""": "isolated",
            "/usr/bin/systemctl is-enabled lx402-api": "enabled",
            "/usr/bin/systemctl is-active lx402-api": "active",
            "/usr/bin/curl -fsS http://127.0.0.1:8090/health": "LX402 healthy",
            """/usr/bin/bash -c 'test $(df --output=pcent /srv/incident | tail -1 | tr -dc 0-9) -lt 70 && echo pressure-cleared'""": "pressure-cleared",
            "/usr/bin/test ! -e /srv/incident/runaway.log": "",
            "/usr/bin/systemctl is-enabled lx402-burner": {"stdout": "disabled", "returncode": 1},
            "/usr/bin/systemctl is-active lx402-burner": {"stdout": "inactive", "returncode": 3},
            """/usr/bin/bash -c 'printf "%s\\n%s\\n" "root cause: disk pressure and runaway burner" "recovery: capacity restored; api enabled and healthy" | diff -u - /var/log/lx402-incident-report >/dev/null && echo exact'""": "exact",
        },
    )


def test_linux_lab_review_blockers_are_hardened() -> None:
    lx101_setup = (LABS / "LX101/setup.sh").read_text(encoding="utf-8")
    lx101_grade = (LABS / "LX101/grade.py").read_text(encoding="utf-8")
    assert "install httpd" not in lx101_setup
    assert "systemd-analyze cat-config systemd/journald.conf" in lx101_grade
    assert "test -d /var/log/journal" in lx101_grade
    assert "journalctl --directory=/var/log/journal" in lx101_grade

    lx102_grade = (LABS / "LX102/grade.py").read_text(encoding="utf-8")
    assert "findmnt -rn -o SOURCE --target /srv/archive" in lx102_grade
    assert "losetup -j /var/lib/labctl-media/lx102-storage.img" in lx102_grade

    lx201_setup = (LABS / "LX201/setup.sh").read_text(encoding="utf-8")
    lx201_grade = (LABS / "LX201/grade.py").read_text(encoding="utf-8")
    assert "/var/lib/labctl/lx201-network-baseline" in lx201_setup
    assert "/var/lib/labctl/lx201-network-baseline" in lx201_grade
    assert "firewall-cmd --reload" in lx201_grade
    assert "--query-service=http" in lx201_grade
    assert "--query-service=cockpit" in lx201_grade

    lx202_setup = (LABS / "LX202/setup.sh").read_text(encoding="utf-8")
    lx202_grade = (LABS / "LX202/grade.py").read_text(encoding="utf-8")
    assert "test -x /usr/local/sbin/lx202-maintenance" in lx202_grade
    assert "Type=oneshot" in lx202_grade
    assert "Unit --value" in lx202_grade
    assert "printf stale > /var/tmp/lx202-cache/stale.tmp" in lx202_grade
    assert "systemctl start lx202-maintenance.service" in lx202_grade
    assert "/var/tmp/lx202-cache" in lx202_setup

    lx301_grade = (LABS / "LX301/grade.py").read_text(encoding="utf-8")
    assert "/etc/selinux/config" in lx301_grade
    assert "semanage permissive -l" in lx301_grade
    assert "stat -c %C /srv/secureweb/index.html" in lx301_grade
    assert "matchpathcon -n /srv/secureweb/index.html" in lx301_grade

    lx302_grade = (LABS / "LX302/grade.py").read_text(encoding="utf-8")
    assert "lx302-pv1.img" in lx302_grade
    assert "lx302-pv2.img" in lx302_grade
    assert "lx302-loop-pvs.service" in lx302_grade
    assert "lx302-boot-baseline" in lx302_grade
    assert "DefaultDependencies=no" in lx302_grade
    assert "local-fs-pre.target" in lx302_grade
    assert "lvm2-monitor.service" in lx302_grade
    assert "WantedBy=sysinit.target" in lx302_grade
    assert "findmnt -rn -M /srv/lvmdata" in lx302_grade
    assert "/etc/fstab" in lx302_grade

    lx401_setup = (LABS / "LX401/setup.sh").read_text(encoding="utf-8")
    lx401_grade = (LABS / "LX401/grade.py").read_text(encoding="utf-8")
    lx401_lab = (LABS / "LX401/lab.yaml").read_text(encoding="utf-8")
    assert "audit=O" in lx401_setup
    assert "audit=0" in lx401_lab
    assert "letter O" in lx401_lab
    assert "lx401-boot-baseline" in lx401_setup
    assert lx401_setup.index("lx401-root-baseline") < lx401_setup.index("passwd -l root")
    assert "lx401-boot-baseline" in lx401_grade
    assert "lx401-recovery.img" in lx401_grade
    assert "/etc/selinux/config" in lx401_grade
    assert "selinux=0" in lx401_grade and "enforcing=0" in lx401_grade
    assert "restorecon -n -v /etc/shadow" in lx401_grade
    assert "matchpathcon -n /etc/shadow" in lx401_grade
    assert "test ! -e /.autorelabel" in lx401_grade
    assert "chcon" in lx401_setup and "/etc/shadow" in lx401_setup
    assert "touch /.autorelabel" in lx401_setup
    assert lx401_setup.index("chcon -t user_tmp_t /etc/shadow") > lx401_setup.index(
        "chown student:student /home/student/LAB.md"
    )
    assert lx401_setup.index("touch /.autorelabel") > lx401_setup.index(
        "chcon -t user_tmp_t /etc/shadow"
    )

    lx402_grade = (LABS / "LX402/grade.py").read_text(encoding="utf-8")
    assert "findmnt -rn -o FSTYPE,SOURCE --target /srv/incident" in lx402_grade
    assert "lx402-incident.img" in lx402_grade
    assert "test ! -e /srv/incident/runaway.log" in lx402_grade
    assert "diff -u" in lx402_grade
