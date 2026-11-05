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

LAB_ID = "LX310"
CHECKS = (
    (
        "learner objective: repaired boot follows a recorded baseline",
        "sudo -n bash -euo pipefail -c 'current=$(cat /proc/sys/kernel/random/boot_id)\n"
        "previous=$(cat /var/lib/labctl/lx310-boot)\n"
        'pattern="^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"\n'
        '[[ "$current" =~ $pattern && "$previous" =~ $pattern ]]\n'
        'test "$current" != "$previous"\n'
        "echo ok'",
        "ok",
        0,
    ),
    (
        "learner objective: new unlocked root credential",
        "sudo -n bash -euo pipefail -c 'baseline=$(cat /var/lib/labctl/lx310-root)\n"
        "current=$(getent shadow root | cut -d: -f2)\n"
        'test -n "$baseline"; test -n "$current"; test "$current" != "$baseline"\n'
        "case \"$current\" in '\"'\"'!'\"'\"'*|'\"'\"'*'\"'\"'*) exit 1;; esac\n"
        "[[ \"$current\" == '\"'\"'$'\"'\"'* ]]\n"
        "echo ok'",
        "ok",
        0,
    ),
    (
        "learner objective: persistent boot entries and normal running target",
        'sudo -n bash -euo pipefail -c \'test "$(systemctl get-default)" = multi-user.target\n'
        "systemctl is-active multi-user.target >/dev/null\n"
        "# Reject every systemd.unit override, regardless of target spelling.\n"
        "if grep -Eq '\"'\"'systemd\\.unit='\"'\"' /etc/default/grub; then exit 1; else test \"$?\" -eq 1; fi\n"
        "if grep -Eq '\"'\"'(^|[[:space:]])systemd\\.unit='\"'\"' /proc/cmdline; then exit 1; else test \"$?\" -eq 1; fi\n"
        "mapfile -t original < /var/lib/labctl/lx310-bls\n"
        'test "${#original[@]}" -gt 0\n'
        'for name in "${original[@]}"; do\n'
        ' test -f "/boot/loader/entries/$name"\n'
        "done\n"
        "entries=(/boot/loader/entries/*.conf)\n"
        'test -f "${entries[0]}"\n'
        'for entry in "${entries[@]}"; do\n'
        ' if grep -Eq \'"\'"\'^options .*systemd\\.unit=\'"\'"\' "$entry"; then exit 1; else test "$?" -eq 1; fi\n'
        "done\n"
        "echo ok'",
        "ok",
        0,
    ),
    (
        "learner objective: original filesystem mounted with preserved ledger",
        "sudo -n bash -euo pipefail -c 'image=/var/lib/labctl-media/lx310.img\n"
        "expected=$(cat /var/lib/labctl/lx310-fs-uuid)\n"
        'actual=$(blkid -p -s UUID -o value "$image")\n'
        'test -n "$expected"; test "$actual" = "$expected"\n'
        'loop=$(losetup -j "$image" --output NAME --noheadings)\n'
        'test -n "$loop"; test "$(printf \'"\'"\'%s\\n\'"\'"\' "$loop" | wc -l)" -eq 1\n'
        "source=$(findmnt -rn -M /srv/lx310 -o SOURCE)\n"
        'test "$(readlink -f "$source")" = "$(readlink -f "$loop")"\n'
        'test "$(findmnt -rn -M /srv/lx310 -o FSTYPE)" = ext4\n'
        "opts=$(findmnt -rn -M /srv/lx310 -o OPTIONS)\n"
        '[[ ",$opts," == *,rw,* ]]\n'
        "sha256sum --check --status /var/lib/labctl/lx310-ledger.sha256\n"
        "echo ok'",
        "ok",
        0,
    ),
    (
        "learner objective: required mount configured for every boot",
        "sudo -n bash -euo pipefail -c 'findmnt --verify --tab-file /etc/fstab >/dev/null\n"
        'awk \'"\'"\'$1 !~ /^#/ && $2=="/srv/lx310" {\n'
        ' count++; if($1!="/var/lib/labctl-media/lx310.img" || $3!="ext4") bad=1;\n'
        ' n=split($4,a,","); loop=0; for(i=1;i<=n;i++) {if(a[i]=="loop")loop=1; '
        'if(a[i]=="nofail"||a[i]=="noauto"||a[i]=="ro")bad=1}; if(!loop)bad=1\n'
        "} END {exit !(count==1 && !bad)}'\"'\"' /etc/fstab\n"
        "echo ok'",
        "ok",
        0,
    ),
    (
        "baseline: enforcing SELinux and repaired credential labels",
        'sudo -n bash -euo pipefail -c \'test "$(getenforce)" = Enforcing\n'
        "grep -Eq '\"'\"'^SELINUX=enforcing$'\"'\"' /etc/selinux/config\n"
        "if grep -Eq '\"'\"'(^|[[:space:]])(selinux=0|enforcing=0)($|[[:space:]])'\"'\"' /proc/cmdline; then exit 1; else test \"$?\" -eq 1; fi\n"
        'test "$(stat -c %C /etc/shadow)" = "$(matchpathcon -n /etc/shadow)"\n'
        "test ! -e /.autorelabel\n"
        "echo ok'",
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
