#!/usr/bin/python3
# ruff: noqa: E501
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX202"
CHECKS = (
    (
        "maintenance program is executable",
        "/usr/bin/test -x /usr/local/sbin/lx202-maintenance",
        "",
    ),
    (
        "maintenance service is oneshot and uses the program",
        "/usr/bin/bash -c 'systemctl cat lx202-maintenance.service | grep -Fx Type=oneshot >/dev/null && "
        "systemctl cat lx202-maintenance.service | grep -Fx ExecStart=/usr/local/sbin/lx202-maintenance >/dev/null && echo valid'",
        "valid",
    ),
    (
        "maintenance timer is enabled",
        "/usr/bin/systemctl is-enabled lx202-maintenance.timer",
        "enabled",
    ),
    (
        "maintenance timer is active",
        "/usr/bin/systemctl is-active lx202-maintenance.timer",
        "active",
    ),
    (
        "missed runs are persistent",
        "/usr/bin/systemctl show lx202-maintenance.timer -p Persistent --value",
        "yes",
    ),
    (
        "timer triggers the maintenance service",
        "/usr/bin/systemctl show lx202-maintenance.timer -p Unit --value",
        "lx202-maintenance.service",
    ),
    (
        "timer uses the required schedule",
        "/usr/bin/bash -c 'systemctl cat lx202-maintenance.timer | grep -Eq "
        '"^OnCalendar=(\\*-[*]-[*] )?02:15(:00)?$" && echo scheduled\'',
        "scheduled",
    ),
    (
        "maintenance service processes freshly staged input",
        "/usr/bin/sudo /usr/bin/bash -c 'install -d -m 0755 /var/tmp/lx202-cache; "
        "printf stale > /var/tmp/lx202-cache/stale.tmp; rm -f /var/log/lx202-maintenance.log; "
        "systemctl start lx202-maintenance.service && echo invoked'",
        "invoked",
    ),
    ("stale cache was removed", "/usr/bin/test ! -e /var/tmp/lx202-cache/stale.tmp", ""),
    (
        "maintenance wrote durable evidence",
        "/usr/bin/cat /var/log/lx202-maintenance.log",
        "maintenance complete",
    ),
)


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("grading context path is required")
        context = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        if context.get("schema_version") != 1 or context.get("lab_id") != LAB_ID:
            raise ValueError("invalid grading context")
        host = context["hosts"]["node"]
        address = host["address"]
        if (
            not isinstance(address, str)
            or not address
            or any(character.isspace() for character in address)
        ):
            raise ValueError("invalid node address")
        ssh = Path(os.environ.get("LABCTL_SSH", "/usr/bin/ssh"))
        identity = Path(host["private_key_path"])
        known_hosts = Path(host["known_hosts_path"])
        if not ssh.is_absolute() or not identity.is_file() or not known_hosts.is_file():
            raise ValueError("absolute SSH and pinned credentials are required")
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {LAB_ID}: {error}", file=sys.stderr)
        return 2

    common = [
        str(ssh),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        str(identity),
        "--",
        f"{host['ssh_user']}@{address}",
    ]
    failed = False
    for description, command, expected in CHECKS:
        result = subprocess.run(  # noqa: S603
            [*common, command], text=True, capture_output=True, check=False
        )
        passed = result.returncode == 0 and result.stdout.strip() == expected
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {description}")
        failed = failed or not passed
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
