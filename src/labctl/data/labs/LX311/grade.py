#!/usr/bin/python3
# ruff: noqa: E501
"""Bounded, pinned, outcome-based guest checks; root is not an anti-cheat boundary."""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX311"
CHECKS = (
    (
        "learner objective: repair follows a recorded reboot",
        "sudo -n bash -euo pipefail -c 'current=$(cat /proc/sys/kernel/random/boot_id)\n"
        "previous=$(cat /var/lib/labctl/lx311-boot)\n"
        'pattern="^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"\n'
        '[[ "$current" =~ $pattern && "$previous" =~ $pattern ]]\n'
        'test "$current" != "$previous"\n'
        "echo ok'",
        "ok",
        0,
    ),
    (
        "baseline: original program and enforcing SELinux",
        "sudo -n bash -euo pipefail -c 'sha256sum --check --status /var/lib/labctl/lx311-program.sha256\n"
        'test "$(getenforce)" = Enforcing\n'
        "grep -Eq '\"'\"'^SELINUX=enforcing$'\"'\"' /etc/selinux/config\n"
        "echo ok'",
        "ok",
        0,
    ),
    (
        "learner objective: original filesystem mounted with preserved ledger",
        "sudo -n bash -euo pipefail -c 'image=/var/lib/labctl-media/lx311.img\n"
        "expected=$(cat /var/lib/labctl/lx311-fs-uuid)\n"
        'actual=$(blkid -p -s UUID -o value "$image")\n'
        'test -n "$expected"; test "$actual" = "$expected"\n'
        'loop=$(losetup -j "$image" --output NAME --noheadings)\n'
        'test -n "$loop"; test "$(printf \'"\'"\'%s\\n\'"\'"\' "$loop" | wc -l)" -eq 1\n'
        "source=$(findmnt -rn -M /srv/lx311 -o SOURCE)\n"
        'test "$(readlink -f "$source")" = "$(readlink -f "$loop")"\n'
        'test "$(findmnt -rn -M /srv/lx311 -o FSTYPE)" = ext4\n'
        "opts=$(findmnt -rn -M /srv/lx311 -o OPTIONS)\n"
        '[[ ",$opts," == *,rw,* ]]\n'
        "sha256sum --check --status /var/lib/labctl/lx311-ledger.sha256\n"
        "echo ok'",
        "ok",
        0,
    ),
    (
        "learner objective: required mount configured for every boot",
        "sudo -n bash -euo pipefail -c 'findmnt --verify --tab-file /etc/fstab >/dev/null\n"
        'awk \'"\'"\'$1 !~ /^#/ && $2=="/srv/lx311" {\n'
        ' count++; if($1!="/var/lib/labctl-media/lx311.img" || $3!="ext4") bad=1;\n'
        ' n=split($4,a,","); loop=0; for(i=1;i<=n;i++) {if(a[i]=="loop")loop=1; '
        'if(a[i]=="nofail"||a[i]=="noauto"||a[i]=="ro")bad=1}; if(!loop)bad=1\n'
        "} END {exit !(count==1 && !bad)}'\"'\"' /etc/fstab\n"
        "echo ok'",
        "ok",
        0,
    ),
    (
        "learner objective: correct daemon owns the externally bound listener",
        'sudo -n bash -euo pipefail -c \'test "$(systemctl is-enabled lx311-export.service)" = enabled\n'
        'test "$(systemctl is-active lx311-export.service)" = active\n'
        'test "$(systemctl show -p User --value lx311-export.service)" = lx311\n'
        'test "$(systemctl show -p NoNewPrivileges --value lx311-export.service)" = yes\n'
        "pid=$(systemctl show -p MainPID --value lx311-export.service)\n"
        'test "$pid" -gt 1\n'
        'test "$(readlink -f /proc/"$pid"/exe)" = "$(readlink -f /usr/bin/python3)"\n'
        "tr '\"'\"'\\0'\"'\"' '\"'\"'\\n'\"'\"' < /proc/\"$pid\"/cmdline | grep -qx "
        "/opt/lx311/export.py\n"
        "ss -H -lntp '\"'\"'sport = :8311'\"'\"' | grep -E '\"'\"'0\\.0\\.0\\.0:8311'\"'\"' | grep "
        '-Eq "pid=$pid,"\n'
        "echo ok'",
        "ok",
        0,
    ),
    (
        "learner objective: a fresh request writes to the supplied medium",
        "sudo -n bash -euo pipefail -c 'loop=$(losetup -j /var/lib/labctl-media/lx311.img --output NAME "
        "--noheadings)\n"
        'test -n "$loop"\n'
        "source=$(findmnt -rn -M /srv/lx311 -o SOURCE)\n"
        'test "$(readlink -f "$source")" = "$(readlink -f "$loop")"\n'
        "rm -f /srv/lx311/last-probe\n"
        "trap '\"'\"'rm -f /srv/lx311/last-probe'\"'\"' EXIT\n"
        "token=$(cat /proc/sys/kernel/random/uuid)\n"
        "reply=$(curl --noproxy '\"'\"'*'\"'\"' --max-time 5 -fsS --data-binary \"$token\" "
        "http://127.0.0.1:8311/probe)\n"
        'test "$reply" = "$token"\n'
        "test ! -L /srv/lx311/last-probe\n"
        'test "$(cat /srv/lx311/last-probe)" = "$token"\n'
        'test "$(stat -c %U /srv/lx311/last-probe)" = lx311\n'
        "echo ok'",
        "ok",
        0,
    ),
    (
        "learner objective: persistent and effective network policy",
        'sudo -n bash -euo pipefail -c \'test "$(systemctl is-enabled firewalld)" = enabled\n'
        'test "$(systemctl is-active firewalld)" = active\n'
        "device=$(ip -o route show default | awk '\"'\"'NR==1 {print $5}'\"'\"')\n"
        'test "$(firewall-cmd --get-zone-of-interface="$device")" = public\n'
        "firewall-cmd --zone=public --query-port=8311/tcp >/dev/null\n"
        "firewall-cmd --permanent --zone=public --query-port=8311/tcp >/dev/null\n"
        "echo ok'",
        "ok",
        0,
    ),
    (
        "learner objective: external client reaches the exporter",
        "http://{address}:8311/health",
        "LX311 retained ledger",
        0,
    ),
)


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("grading context required")
        context = json.loads(Path(sys.argv[1]).read_text())
        if context.get("schema_version") != 1 or context.get("lab_id") != LAB_ID:
            raise ValueError("invalid grading context")
        host = context["hosts"]["node"]
        address = str(ipaddress.ip_address(host["address"]))
        if host["ssh_user"] != "student":
            raise ValueError("student identity required")
        key = Path(host["private_key_path"])
        pins = Path(host["known_hosts_path"])
        ssh = Path(os.environ.get("LABCTL_SSH", "/usr/bin/ssh"))
        if not ssh.is_absolute() or not key.is_file() or not pins.is_file():
            raise ValueError("absolute SSH and pinned credentials required")
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"ERROR {LAB_ID}: {error}", file=sys.stderr)
        return 2
    common = [
        str(ssh),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={pins}",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        "-i",
        str(key),
        "--",
        f"student@{address}",
    ]
    failed = False
    for description, command, expected, expected_rc in CHECKS:
        try:
            argv = [*common, command]
            if command.startswith("http://{address}:"):
                remote_address = f"[{address}]" if ":" in address else address
                argv = [
                    "/usr/bin/curl",
                    "--noproxy",
                    "*",
                    "--connect-timeout",
                    "5",
                    "--max-time",
                    "10",
                    "-fsS",
                    command.format(address=remote_address),
                ]
            result = subprocess.run(  # noqa: S603 - fixed checks and validated address
                argv, text=True, capture_output=True, check=False, timeout=45
            )
            passed = result.returncode == expected_rc and result.stdout.strip() == expected
        except (OSError, subprocess.TimeoutExpired):
            passed = False
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {description}")
        failed |= not passed
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
