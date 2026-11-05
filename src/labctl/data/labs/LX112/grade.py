#!/usr/bin/python3
# ruff: noqa: E501
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX112"
CHECKS = {
    "persistent and running hostname": (
        '/usr/bin/sudo -n /usr/bin/bash -o pipefail -c \'if { test "$(hostnamectl --static)" = workshop.lab && test "$(hostname)" = workshop.lab; }; then printf "ok\\n"; else exit 1; fi\'',
        "ok",
    ),
    "persistent autoconnect dummy profile": (
        '/usr/bin/sudo -n /usr/bin/bash -o pipefail -c \'if { test "$(nmcli --escape no -g connection.type connection show lx112-dummy)" = dummy && test "$(nmcli --escape no -g connection.interface-name connection show lx112-dummy)" = labdummy0 && test "$(nmcli --escape no -g connection.autoconnect connection show lx112-dummy)" = yes && nmcli -t -f NAME,FILENAME connection show | python3 -c \'"\'"\'import sys; rows=[x.rstrip("\\n").split(":",1)[1] for x in sys.stdin if x.startswith("lx112-dummy:")]; assert len(rows)==1 and rows[0].startswith("/etc/NetworkManager/system-connections/") and len(rows[0])>len("/etc/NetworkManager/system-connections/")\'"\'"\'; }; then printf "ok\\n"; else exit 1; fi\'',
        "ok",
    ),
    "persistent IPv4 address and isolation": (
        '/usr/bin/sudo -n /usr/bin/bash -o pipefail -c \'if { test "$(nmcli --escape no -g ipv4.method connection show lx112-dummy)" = manual && test "$(nmcli --escape no -g ipv4.addresses connection show lx112-dummy)" = 192.0.2.20/24 && test "$(nmcli --escape no -g ipv4.never-default connection show lx112-dummy)" = yes && gateway=$(nmcli --escape no -g ipv4.gateway connection show lx112-dummy) && test -z "$gateway"; }; then printf "ok\\n"; else exit 1; fi\'',
        "ok",
    ),
    "persistent IPv6 address and isolation": (
        '/usr/bin/sudo -n /usr/bin/bash -o pipefail -c \'if { test "$(nmcli --escape no -g ipv6.method connection show lx112-dummy)" = manual && test "$(nmcli --escape no -g ipv6.addresses connection show lx112-dummy)" = 2001:db8:112::20/64 && test "$(nmcli --escape no -g ipv6.never-default connection show lx112-dummy)" = yes && gateway=$(nmcli --escape no -g ipv6.gateway connection show lx112-dummy) && test -z "$gateway"; }; then printf "ok\\n"; else exit 1; fi\'',
        "ok",
    ),
    "active dummy and both addresses": (
        '/usr/bin/sudo -n /usr/bin/bash -o pipefail -c \'if { test "$(nmcli --escape no -g GENERAL.CONNECTION device show labdummy0)" = lx112-dummy && ip -d link show dev labdummy0 | grep -w dummy >/dev/null && ip -o -4 address show dev labdummy0 | grep -F "inet 192.0.2.20/24 " >/dev/null && ip -o -6 address show dev labdummy0 | grep -F "inet6 2001:db8:112::20/64 " >/dev/null; }; then printf "ok\\n"; else exit 1; fi\'',
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
            raise ValueError("grading context does not describe LX112 schema v1")
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
