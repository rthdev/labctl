#!/usr/bin/python3
# ruff: noqa: E501
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX402"
CHECKS = (
    (
        "incident filesystem uses the isolated ext4 backing image",
        "/usr/bin/sudo /usr/bin/bash -c 'set -- $(findmnt -rn -o FSTYPE,SOURCE --target /srv/incident); "
        "image_loop=$(losetup -j /var/lib/labctl-media/lx402-incident.img | head -n 1 | cut -d: -f1); "
        'test "$1" = ext4 && test -n "$image_loop" && test "$2" = "$image_loop" && echo isolated\'',
        "isolated",
    ),
    ("API is enabled for durable recovery", "/usr/bin/systemctl is-enabled lx402-api", "enabled"),
    ("API is active", "/usr/bin/systemctl is-active lx402-api", "active"),
    (
        "health endpoint responds",
        "/usr/bin/curl -fsS http://127.0.0.1:8090/health",
        "LX402 healthy",
    ),
    (
        "disk pressure is below threshold",
        "/usr/bin/bash -c 'test $(df --output=pcent /srv/incident | tail -1 | tr -dc 0-9) "
        "-lt 70 && echo pressure-cleared'",
        "pressure-cleared",
    ),
    ("runaway log was removed", "/usr/bin/test ! -e /srv/incident/runaway.log", ""),
    ("runaway burner is disabled", "/usr/bin/systemctl is-enabled lx402-burner", "disabled", 1),
    ("runaway burner is inactive", "/usr/bin/systemctl is-active lx402-burner", "inactive", 3),
    (
        "incident report contains exactly the required evidence",
        '/usr/bin/bash -c \'printf "%s\\n%s\\n" "root cause: disk pressure and runaway burner" '
        '"recovery: capacity restored; api enabled and healthy" | '
        "diff -u - /var/log/lx402-incident-report >/dev/null && echo exact'",
        "exact",
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
    for check in CHECKS:
        description, command, expected = check[:3]
        expected_returncode = check[3] if len(check) == 4 else 0
        result = subprocess.run(  # noqa: S603
            [*common, command], text=True, capture_output=True, check=False
        )
        passed = result.returncode == expected_returncode and result.stdout.strip() == expected
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {description}")
        failed = failed or not passed
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
