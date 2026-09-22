#!/usr/bin/python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX001"
CHECKS = {
    "group labops exists with opsadmin as a member": ("getent group labops", "opsadmin"),
    "opsadmin belongs to labops": ("id -nG opsadmin", "labops"),
    "shared directory ownership and mode": (
        "stat -c '%U:%G %a' /srv/labshare",
        "opsadmin:labops 2770",
    ),
    "operations file ownership and mode": (
        "sudo stat -c '%U:%G %a' /srv/labshare/operations.txt",
        "opsadmin:labops 660",
    ),
}


def matches(command: str, expected: str, output: str) -> bool:
    if command == "getent group labops":
        fields = output.split(":")
        return len(fields) == 4 and fields[0] == "labops" and expected in fields[3].split(",")
    if command == "id -nG opsadmin":
        return expected in output.split()
    return output == expected


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("grading context path must be the first argument")
        context_path = Path(sys.argv[1])
        context = json.loads(context_path.read_text(encoding="utf-8"))
        if context.get("schema_version") != 1 or context.get("lab_id") != LAB_ID:
            raise ValueError("grading context does not describe LX001 schema v1")
        host = context["hosts"]["node"]
        address = host["address"]
        if not isinstance(address, str) or not address or any(char.isspace() for char in address):
            raise ValueError("node address is invalid")
        ssh = Path(os.environ.get("LABCTL_SSH", "/usr/bin/ssh"))
        identity = Path(host["private_key_path"])
        known_hosts = Path(host["known_hosts_path"])
        if not ssh.is_absolute() or not identity.is_file() or not known_hosts.is_file():
            raise ValueError(
                "absolute SSH executable, identity, and pinned known_hosts are required"
            )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {LAB_ID}: {error}", file=sys.stderr)
        return 2

    failed = False
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
    for description, (command, expected) in CHECKS.items():
        # All destinations and commands are validated or fixed above; no shell is used.
        result = subprocess.run(  # noqa: S603
            [*common, command], text=True, capture_output=True, check=False
        )
        passed = result.returncode == 0 and matches(command, expected, result.stdout.strip())
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {description}")
        failed = failed or not passed
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
