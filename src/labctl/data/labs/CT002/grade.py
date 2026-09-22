#!/usr/bin/python3
# ruff: noqa: S603
from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

LAB_ID = "CT002"
CHECKS = (
    ("rootless Podman store and SELinux", "/usr/bin/podman info --format json", "info"),
    (
        "worker is running with the exact image and command",
        "/usr/bin/podman inspect ct002-worker",
        "inspect",
    ),
    (
        "exec-created state is exact",
        "/usr/bin/podman exec ct002-worker cat /lesson/status",
        "status",
    ),
    ("startup log is retained", "/usr/bin/podman logs ct002-worker", "log"),
)


def valid(kind: str, text: str) -> bool:
    try:
        if kind == "status":
            return text.strip() == "lifecycle inspected"
        if kind == "log":
            return bool(text.splitlines()) and set(text.splitlines()) == {"CT002 worker ready"}
        data: Any = json.loads(text)
        if kind == "info":
            return (
                data["host"]["security"]["rootless"] is True
                and data["host"]["security"]["selinuxEnabled"] is True
                and data["store"]["graphRoot"] == "/home/student/.local/share/containers/storage"
            )
        item = data[0]
        return (
            len(data) == 1
            and item["Name"] == "ct002-worker"
            and item["State"]["Status"] == "running"
            and item["Config"]["Image"] == "docker.io/library/alpine:3.20"
            and item["Config"]["Cmd"]
            == ["sh", "-c", "echo 'CT002 worker ready'; exec sleep infinity"]
            and item["HostConfig"]["Privileged"] is False
            and ":container_t:" in item["ProcessLabel"]
        )
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return False


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("context path required")
        context = json.loads(Path(sys.argv[1]).read_text())
        if (
            not isinstance(context, dict)
            or context.get("schema_version") != 1
            or context.get("lab_id") != LAB_ID
        ):
            raise ValueError("wrong context")
        hosts = context.get("hosts")
        if not isinstance(hosts, dict) or set(hosts) != {"node"}:
            raise ValueError("exactly node required")
        host = hosts["node"]
        if not isinstance(host, dict) or host.get("ssh_user") != "student":
            raise ValueError("student host required")
        ipaddress.ip_address(host["address"])
        identity, known = Path(host["private_key_path"]), Path(host["known_hosts_path"])
        ssh = Path(os.environ.get("LABCTL_SSH", "/usr/bin/ssh"))
        if (
            not ssh.is_absolute()
            or not ssh.is_file()
            or not os.access(ssh, os.X_OK)
            or not identity.is_absolute()
            or not identity.is_file()
            or not known.is_absolute()
            or not known.is_file()
        ):
            raise ValueError("pinned SSH inputs required")
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
        "ConnectTimeout=10",
        "-o",
        f"UserKnownHostsFile={known}",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        str(identity),
        "--",
        f"student@{host['address']}",
    ]
    failed = False
    for description, command, kind in CHECKS:
        try:
            result = subprocess.run(
                [*common, command], text=True, capture_output=True, check=False, timeout=15
            )
            passed = result.returncode == 0 and valid(kind, result.stdout)
        except (OSError, subprocess.TimeoutExpired):
            passed = False
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {description}")
        failed |= not passed
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
