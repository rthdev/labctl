#!/usr/bin/python3
# ruff: noqa: E501
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX101"
CHECKS = (
    (
        "httpd package is installed",
        "/usr/bin/bash -c 'rpm -q httpd >/dev/null && echo installed'",
        "installed",
    ),
    ("httpd is enabled", "/usr/bin/systemctl is-enabled httpd", "enabled"),
    ("httpd is active", "/usr/bin/systemctl is-active httpd", "active"),
    ("health endpoint responds", "/usr/bin/curl -fsS http://127.0.0.1/health", "LX101 healthy"),
    (
        "journald persistence is effective",
        "/usr/bin/sudo /usr/bin/bash -c 'systemd-analyze cat-config systemd/journald.conf | "
        'awk -F= "/^[[:space:]]*Storage[[:space:]]*=/{value=\\$2} END {gsub(/[[:space:]]/, "", value); '
        'if (value == "persistent") print value}"\'',
        "persistent",
    ),
    (
        "persistent journal storage is operating",
        "/usr/bin/sudo /usr/bin/bash -c 'test -d /var/log/journal && "
        "journalctl --directory=/var/log/journal --quiet -u httpd -n 1 | grep -q . && echo operating'",
        "operating",
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
