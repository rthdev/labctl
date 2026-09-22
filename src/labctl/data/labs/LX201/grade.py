#!/usr/bin/python3
# ruff: noqa: E501
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX201"
CHECKS = (
    ("NetworkManager remains active", "/usr/bin/systemctl is-active NetworkManager", "active"),
    (
        "managed connection and address are preserved",
        "/usr/bin/sudo /usr/bin/bash -c 'read -r device < /var/lib/labctl/lx201-network-baseline; "
        "uuid=$(sed -n 2p /var/lib/labctl/lx201-network-baseline); "
        "address=$(sed -n 3p /var/lib/labctl/lx201-network-baseline); "
        'connection=$(nmcli -g GENERAL.CONNECTION device show "$device") && '
        'test "$(nmcli -g connection.uuid connection show "$connection")" = "$uuid" && '
        'test "$(nmcli -g IP4.ADDRESS device show "$device" | head -n 1)" = "$address" && echo preserved\'',
        "preserved",
    ),
    ("firewalld is enabled", "/usr/bin/systemctl is-enabled firewalld", "enabled"),
    ("firewalld is active", "/usr/bin/systemctl is-active firewalld", "active"),
    (
        "firewall reload succeeds",
        "/usr/bin/sudo /usr/bin/bash -c 'firewall-cmd --reload >/dev/null && echo reloaded'",
        "reloaded",
    ),
    (
        "HTTP is permanently allowed",
        "/usr/bin/sudo /usr/bin/firewall-cmd --permanent --zone=public --query-service=http",
        "yes",
    ),
    (
        "cockpit is not exposed",
        "/usr/bin/sudo /usr/bin/firewall-cmd --permanent --zone=public --query-service=cockpit",
        "no",
        1,
    ),
    (
        "HTTP is allowed after reload",
        "/usr/bin/sudo /usr/bin/firewall-cmd --zone=public --query-service=http",
        "yes",
    ),
    (
        "cockpit remains closed after reload",
        "/usr/bin/sudo /usr/bin/firewall-cmd --zone=public --query-service=cockpit",
        "no",
        1,
    ),
    (
        "network health endpoint responds",
        "/usr/bin/curl -fsS http://127.0.0.1/network-health",
        "LX201 reachable",
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
