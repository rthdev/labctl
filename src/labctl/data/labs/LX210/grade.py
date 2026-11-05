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

LAB_ID = "LX210"
CHECKS = (
    (
        "learner objective: repaired state survives a reboot",
        'sudo -n bash -euo pipefail -c \'current=$(cat /proc/sys/kernel/random/boot_id)\nprevious=$(cat /var/lib/labctl/lx210-boot)\npattern="^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"\n[[ "$current" =~ $pattern && "$previous" =~ $pattern ]]\ntest "$current" != "$previous"\necho ok\'',
        "ok",
        0,
    ),
    (
        "learner objective: XFS on the exact supplied LVM backing",
        'sudo -n bash -euo pipefail -c \'source=$(findmnt -nro SOURCE --mountpoint /srv/lx210)\ntest "$(readlink -f "$source")" = "$(readlink -f /dev/vg210/records)"\nloop=$(losetup -j /var/lib/labctl-media/lx210.img --output NAME --noheadings)\ntest -n "$loop"; test "$(printf \'"\'"\'%s\\n\'"\'"\' "$loop" | wc -l)" = 1\ntest "$(pvs --devices "$loop" --noheadings -o vg_name "$loop" | xargs)" = vg210\ntest "$(vgs --devices "$loop" --noheadings -o pv_count vg210 2>/dev/null | xargs)" = 1\ntest "$(findmnt -nro FSTYPE --mountpoint /srv/lx210)" = xfs\noptions=$(findmnt -nro OPTIONS --mountpoint /srv/lx210)\n[[ ",$options," == *,rw,* ]]\necho ok\'',
        "ok",
        0,
    ),
    (
        "learner objective: capacity and persistent mount",
        'sudo -n bash -euo pipefail -c \'loop=$(losetup -j /var/lib/labctl-media/lx210.img --output NAME --noheadings)\nbytes=$(lvs --devices "$loop" --noheadings --units b --nosuffix -o lv_size /dev/vg210/records | xargs)\nawk -v n="$bytes" \'"\'"\'BEGIN {exit !(n >= 402653184 && n <= 536870912)}\'"\'"\'\nsource=$(findmnt --fstab --evaluate --mountpoint /srv/lx210 -nro SOURCE)\ntest "$(findmnt --fstab --mountpoint /srv/lx210 -nro FSTYPE)" = xfs\ntest "$(readlink -f "$source")" = "$(readlink -f /dev/vg210/records)"\noptions=$(findmnt --fstab --mountpoint /srv/lx210 -nro OPTIONS)\nif printf \'"\'"\'%s\\n\'"\'"\' "$options" | tr , \'"\'"\'\\n\'"\'"\' | grep -Eq \'"\'"\'^(ro|noauto|nofail)$\'"\'"\'; then exit 1; else statuses=("${PIPESTATUS[@]}"); test "${statuses[*]}" = "0 0 1"; fi\necho ok\'',
        "ok",
        0,
    ),
    (
        "learner objective: mounted archive ownership and contents",
        'sudo -n bash -euo pipefail -c \'source=$(findmnt -nro SOURCE --mountpoint /srv/lx210)\ntest "$(readlink -f "$source")" = "$(readlink -f /dev/vg210/records)"\nloop=$(losetup -j /var/lib/labctl-media/lx210.img --output NAME --noheadings)\ntest -n "$loop"; test "$(printf \'"\'"\'%s\\n\'"\'"\' "$loop" | wc -l)" = 1\ntest "$(pvs --devices "$loop" --noheadings -o vg_name "$loop" | xargs)" = vg210\ntest "$(vgs --devices "$loop" --noheadings -o pv_count vg210 2>/dev/null | xargs)" = 1\ntest "$(cat /srv/lx210/manifest)" = \'"\'"\'LX210 persistent records\'"\'"\'\ntest "$(stat -c \'"\'"\'%U:%G %a\'"\'"\' /srv/lx210/manifest)" = \'"\'"\'student:student 640\'"\'"\'\necho ok\'',
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
            result = subprocess.run(  # noqa: S603 - fixed checks and validated address
                [*common, command], text=True, capture_output=True, check=False, timeout=45
            )
            passed = result.returncode == expected_rc and result.stdout.strip() == expected
        except (OSError, subprocess.TimeoutExpired):
            passed = False
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {description}")
        failed |= not passed
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
