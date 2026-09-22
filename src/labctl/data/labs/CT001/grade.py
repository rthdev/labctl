#!/usr/bin/python3
# ruff: noqa: S603
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

LAB_ID = "CT001"
COMMANDS = (
    (
        "rootless Podman uses the student store with SELinux enabled",
        "/usr/bin/podman info --format json",
        "info",
    ),
    (
        "one-shot container has the exact successful configuration",
        "/usr/bin/podman inspect ct001-hello",
        "inspect",
    ),
    ("container output is exact", "/usr/bin/podman logs ct001-hello", "log"),
)
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _host(context: object) -> dict[str, str]:
    if (
        not isinstance(context, dict)
        or context.get("schema_version") != 1
        or context.get("lab_id") != LAB_ID
    ):
        raise ValueError("grading context does not describe CT001 schema v1")
    hosts = context.get("hosts")
    if not isinstance(hosts, dict) or set(hosts) != {"node"} or not isinstance(hosts["node"], dict):
        raise ValueError("grading context must contain exactly node")
    host = hosts["node"]
    required = ("address", "ssh_user", "private_key_path", "known_hosts_path")
    if not all(isinstance(host.get(key), str) and host[key] for key in required):
        raise ValueError("node context is incomplete")
    ipaddress.ip_address(host["address"])
    if host["ssh_user"] != "student" or not _USER.fullmatch(host["ssh_user"]):
        raise ValueError("node must use the student SSH account")
    for key in ("private_key_path", "known_hosts_path"):
        path = Path(host[key])
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f"{key} is invalid")
    return {key: host[key] for key in required}


def _valid(kind: str, output: str) -> bool:
    if kind == "log":
        return output.strip() == "CT001 container complete"
    try:
        value: Any = json.loads(output)
        if kind == "info":
            return (
                value["host"]["security"]["rootless"] is True
                and value["host"]["security"]["selinuxEnabled"] is True
                and value["store"]["graphRoot"] == "/home/student/.local/share/containers/storage"
            )
        item = value[0]
        return (
            len(value) == 1
            and item["Name"] == "ct001-hello"
            and item["State"]["Status"] == "exited"
            and item["State"]["ExitCode"] == 0
            and item["Config"]["Image"] == "docker.io/library/alpine:3.20"
            and item["Config"]["Cmd"] == ["printf", "CT001 container complete\\n"]
            and item["HostConfig"]["Privileged"] is False
            and ":container_t:" in item["ProcessLabel"]
        )
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return False


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("grading context path must be the first argument")
        context = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        host = _host(context)
        ssh = Path(os.environ.get("LABCTL_SSH", "/usr/bin/ssh"))
        if not ssh.is_absolute() or not ssh.is_file() or not os.access(ssh, os.X_OK):
            raise ValueError("absolute SSH executable is required")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {LAB_ID}: {error}", file=sys.stderr)
        return 2
    common = [
        str(ssh),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        f"UserKnownHostsFile={host['known_hosts_path']}",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        host["private_key_path"],
        "--",
        f"student@{host['address']}",
    ]
    failed = False
    for description, command, kind in COMMANDS:
        try:
            result = subprocess.run(
                [*common, command], text=True, capture_output=True, check=False, timeout=15
            )
            passed = result.returncode == 0 and _valid(kind, result.stdout)
        except (OSError, subprocess.TimeoutExpired):
            passed = False
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {description}")
        failed |= not passed
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
