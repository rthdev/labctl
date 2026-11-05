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

LAB_ID = "LX312"
CHECKS = (
    (
        "learner objective: repaired state survives a reboot",
        'sudo -n bash -euo pipefail -c \'current=$(cat /proc/sys/kernel/random/boot_id)\nprevious=$(cat /var/lib/labctl/lx312-boot)\npattern="^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"\n[[ "$current" =~ $pattern && "$previous" =~ $pattern ]]\ntest "$current" != "$previous"\necho ok\'',
        "ok",
        0,
    ),
    (
        "baseline: supplied workload and security unchanged",
        "sudo -n bash -euo pipefail -c 'sha256sum --check --status /var/lib/labctl/lx312-original.sha256\ntest \"$(getenforce)\" = Enforcing\necho ok'",
        "ok",
        0,
    ),
    (
        "learner objective: bounded workload starts without privilege bypass",
        'sudo -n bash -euo pipefail -c \'test "$(systemctl is-enabled lx312-app.service)" = enabled\ntest "$(systemctl is-active lx312-app.service)" = active\ntest "$(systemctl show -p User --value lx312-app.service)" = lx312\ntest "$(systemctl show -p NoNewPrivileges --value lx312-app.service)" = yes\nmax=$(systemctl show -p MemoryMax --value lx312-app.service)\n[[ "$max" =~ ^[0-9]+$ ]]; test "$max" -ge 100663296; test "$max" -le 201326592\ntest "$(systemctl show -p MemorySwapMax --value lx312-app.service)" = 0\ncurrent=$(systemctl show -p MemoryCurrent --value lx312-app.service)\n[[ "$current" =~ ^[0-9]+$ ]]; test "$current" -ge 50331648; test "$current" -le 134217728\necho ok\'',
        "ok",
        0,
    ),
    (
        "learner objective: actual process owns the responding socket",
        'sudo -n bash -euo pipefail -c \'pid=$(systemctl show -p MainPID --value lx312-app.service)\ntest "$pid" -gt 1\ntest "$(readlink -f /proc/"$pid"/exe)" = "$(readlink -f /usr/bin/python3)"\ntr \'"\'"\'\\0\'"\'"\' \'"\'"\'\\n\'"\'"\' < /proc/"$pid"/cmdline | grep -qx /opt/lx312/app.py\nuid=$(id -u lx312)\nawk -v uid="$uid" \'"\'"\'$1=="Uid:" {exit !($2==uid && $3==uid && $4==uid && $5==uid)}\'"\'"\' /proc/"$pid"/status\nss -lntp \'"\'"\'sport = :8312\'"\'"\' | grep -Eq "pid=$pid,"\nhash=$(sha256sum /srv/lx312/dataset | cut -d\'"\'"\' \'"\'"\' -f1)\ntest "$(curl --noproxy \'"\'"\'*\'"\'"\' --max-time 5 -fsS http://127.0.0.1:8312/health)" = "bytes=50331648 sha256=$hash"\necho ok\'',
        "ok",
        0,
    ),
    (
        "learner objective: least-privilege persistent dataset access",
        "sudo -n bash -euo pipefail -c 'test \"$(stat -c '\"'\"'%U:%G %a'\"'\"' /srv/lx312/dataset)\" = '\"'\"'root:lx312 640'\"'\"'\nrunuser -u lx312 -- test -r /srv/lx312/dataset\nif runuser -u lx312 -- test -w /srv/lx312/dataset; then exit 1; fi\necho ok'",
        "ok",
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
