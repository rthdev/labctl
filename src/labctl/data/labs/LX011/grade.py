#!/usr/bin/python3
# ruff: noqa: E501
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX011"
CHECKS = {
    "error lines": (
        "/usr/bin/sudo -n /usr/bin/bash -o pipefail -c 'if { test -f /home/student/lx011/errors.txt && test ! -L /home/student/lx011/errors.txt && printf '\"'\"'ERROR disk\\nERROR network\\n'\"'\"' | cmp -s - /home/student/lx011/errors.txt; }; then printf \"ok\\n\"; else exit 1; fi'",
        "ok",
    ),
    "unique levels": (
        "/usr/bin/sudo -n /usr/bin/bash -o pipefail -c 'if { test -f /home/student/lx011/levels.txt && test ! -L /home/student/lx011/levels.txt && printf '\"'\"'ERROR\\nINFO\\nWARN\\n'\"'\"' | cmp -s - /home/student/lx011/levels.txt; }; then printf \"ok\\n\"; else exit 1; fi'",
        "ok",
    ),
    "level counts": (
        "/usr/bin/sudo -n /usr/bin/bash -o pipefail -c 'if { test -f /home/student/lx011/counts.txt && test ! -L /home/student/lx011/counts.txt && printf '\"'\"'ERROR 2\\nINFO 2\\nWARN 2\\n'\"'\"' | cmp -s - /home/student/lx011/counts.txt; }; then printf \"ok\\n\"; else exit 1; fi'",
        "ok",
    ),
    "numbered source": (
        "/usr/bin/sudo -n /usr/bin/bash -o pipefail -c 'if { test -f /home/student/lx011/numbered.txt && test ! -L /home/student/lx011/numbered.txt && printf '\"'\"'1:INFO boot\\n2:ERROR disk\\n3:WARN load\\n4:ERROR network\\n5:INFO ready\\n6:WARN memory\\n'\"'\"' | cmp -s - /home/student/lx011/numbered.txt; }; then printf \"ok\\n\"; else exit 1; fi'",
        "ok",
    ),
}


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("grading context path must be the first argument")
        context_path = Path(sys.argv[1])
        context = json.loads(context_path.read_text(encoding="utf-8"))
        if context.get("schema_version") != 1 or context.get("lab_id") != LAB_ID:
            raise ValueError("grading context does not describe LX011 schema v1")
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
        "ConnectTimeout=10",
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
        try:
            result = subprocess.run(  # noqa: S603
                [*common, command], text=True, capture_output=True, check=False, timeout=30
            )
            passed = result.returncode == 0 and result.stdout.strip() == expected
        except (OSError, subprocess.TimeoutExpired):
            passed = False
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {description}")
        failed = failed or not passed
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
