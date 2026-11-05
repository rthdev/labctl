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

LAB_ID = "LX211"
CHECKS = (
    (
        "learner objective: repaired state survives a reboot",
        'sudo -n bash -euo pipefail -c \'current=$(cat /proc/sys/kernel/random/boot_id)\nprevious=$(cat /var/lib/labctl/lx211-boot)\npattern="^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"\n[[ "$current" =~ $pattern && "$previous" =~ $pattern ]]\ntest "$current" != "$previous"\necho ok\'',
        "ok",
        0,
    ),
    (
        "baseline: SELinux enforcing now and persistently",
        'sudo -n bash -euo pipefail -c \'test "$(getenforce)" = Enforcing\ngrep -Eq \'"\'"\'^SELINUX=enforcing$\'"\'"\' /etc/selinux/config\ntest "$(getsebool httpd_unified)" = \'"\'"\'httpd_unified --> off\'"\'"\'\npermissive=$(semanage permissive -l)\nif printf \'"\'"\'%s\\n\'"\'"\' "$permissive" | grep -Eq \'"\'"\'^[[:space:]]*httpd_t[[:space:]]*$\'"\'"\'; then exit 1; else statuses=("${PIPESTATUS[@]}"); test "${statuses[*]}" = "0 1"; fi\necho ok\'',
        "ok",
        0,
    ),
    (
        "learner objective: persistent policy labels and HTTP port",
        'sudo -n bash -euo pipefail -c \'for path in /srv/lx211/site /srv/lx211/site/health; do\n    test "$(stat -c %C "$path" | cut -d: -f3)" = httpd_sys_content_t\n    test "$(matchpathcon -n "$path" | cut -d: -f3)" = httpd_sys_content_t\ndone\nsemanage port -l | awk \'"\'"\'$1=="http_port_t" && $2=="tcp" {for(i=3;i<=NF;i++) {gsub(/,/,"",$i); if ($i==8088) found=1}} END {exit !found}\'"\'"\'\necho ok\'',
        "ok",
        0,
    ),
    (
        "learner objective: enabled Apache serves the required tree",
        'sudo -n bash -euo pipefail -c \'test "$(systemctl is-enabled httpd)" = enabled\ntest "$(systemctl is-active httpd)" = active\ntest ! -L /srv/lx211/site; test ! -L /srv/lx211/site/health\ntest "$(cat /srv/lx211/site/health)" = \'"\'"\'LX211 secured records\'"\'"\'\ntoken=$(cat /proc/sys/kernel/random/uuid)\nfile=/srv/lx211/site/probe-$token\ntrap \'"\'"\'rm -f "$file"\'"\'"\' EXIT\nprintf \'"\'"\'%s\'"\'"\' "$token" > "$file"\nrestorecon "$file"\ntest "$(curl --noproxy \'"\'"\'*\'"\'"\' --max-time 5 -fsS "http://127.0.0.1:8088/probe-$token")" = "$token"\necho ok\'',
        "ok",
        0,
    ),
    (
        "learner objective: runtime and permanent ingress policy",
        'sudo -n bash -euo pipefail -c \'test "$(systemctl is-enabled firewalld)" = enabled\ntest "$(systemctl is-active firewalld)" = active\ndevice=$(ip -o route show default | awk \'"\'"\'NR==1 {print $5}\'"\'"\')\ntest "$(firewall-cmd --get-zone-of-interface="$device")" = public\nfirewall-cmd --zone=public --query-port=8088/tcp >/dev/null\nfirewall-cmd --permanent --zone=public --query-port=8088/tcp >/dev/null\necho ok\'',
        "ok",
        0,
    ),
    (
        "learner objective: cockpit absent at runtime",
        "sudo -n bash -euo pipefail -c 'firewall-cmd --zone=public --query-service=cockpit'",
        "no",
        1,
    ),
    (
        "learner objective: cockpit absent persistently",
        "sudo -n bash -euo pipefail -c 'firewall-cmd --permanent --zone=public --query-service=cockpit'",
        "no",
        1,
    ),
    (
        "learner objective: host reaches the secured service",
        "http://{address}:8088/health",
        "LX211 secured records",
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
