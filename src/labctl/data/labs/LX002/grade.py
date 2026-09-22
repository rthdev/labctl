#!/usr/bin/python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX002"
CHECKS = (
    (
        "source and archive hierarchy exists",
        "/usr/bin/test -d /srv/lx002/archive -a -f /srv/lx002/source/records.txt",
        "",
    ),
    (
        "hard link shares the source inode",
        "/usr/bin/test /srv/lx002/source/records.txt -ef /srv/lx002/archive/records.hard",
        "",
    ),
    (
        "symbolic link resolves to the source",
        "/usr/bin/readlink -f /home/student/current-records",
        "/srv/lx002/source/records.txt",
    ),
    (
        "report contains the required aggregate",
        "/usr/bin/cat /srv/lx002/report.txt",
        "ERROR 2\nINFO 2\nWARN 2",
    ),
    (
        "report ownership and mode are correct",
        "/usr/bin/stat -c '%U:%G %a' /srv/lx002/report.txt",
        "student:student 640",
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
