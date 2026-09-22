#!/usr/bin/python3
# ruff: noqa: S603
from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "CT101"
CHECKS = (
    (
        "published loopback endpoint responds",
        "/usr/bin/curl --noproxy '*' --max-time 5 -fsS http://127.0.0.1:8080/index.html",
        "probe",
    ),
    ("rootless SELinux Podman store", "/usr/bin/podman info --format json", "info"),
    ("web container and loopback port are exact", "/usr/bin/podman inspect ct101-web", "inspect"),
    (
        "content is served by the inspected container",
        "/usr/bin/podman exec ct101-web wget -qO- http://127.0.0.1:8080/index.html",
        "probe",
    ),
)


def valid(kind: str, text: str) -> bool:
    try:
        if kind == "probe":
            return text.strip() == "CT101 rootless web"
        data = json.loads(text)
        if kind == "info":
            return (
                data["host"]["security"]["rootless"] is True
                and data["host"]["security"]["selinuxEnabled"] is True
                and data["store"]["graphRoot"] == "/home/student/.local/share/containers/storage"
            )
        x = data[0]
        binding = x["HostConfig"]["PortBindings"]
        return (
            len(data) == 1
            and x["Name"] == "ct101-web"
            and x["State"]["Status"] == "running"
            and x["Config"]["Image"] == "docker.io/library/alpine:3.20"
            and x["HostConfig"]["Privileged"] is False
            and ":container_t:" in x["ProcessLabel"]
            and binding == {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}
        )
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return False


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("context path required")
        c = json.loads(Path(sys.argv[1]).read_text())
        h = c.get("hosts") if isinstance(c, dict) else None
        if (
            c.get("schema_version") != 1
            or c.get("lab_id") != LAB_ID
            or not isinstance(h, dict)
            or set(h) != {"node"}
            or h["node"].get("ssh_user") != "student"
        ):
            raise ValueError("invalid pinned context")
        n = h["node"]
        ipaddress.ip_address(n["address"])
        ssh = Path(os.environ.get("LABCTL_SSH", "/usr/bin/ssh"))
        key = Path(n["private_key_path"])
        known = Path(n["known_hosts_path"])
        if not all(p.is_absolute() and p.is_file() for p in (ssh, key, known)) or not os.access(
            ssh, os.X_OK
        ):
            raise ValueError("invalid SSH inputs")
    except (AttributeError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as e:
        print(f"ERROR {LAB_ID}: {e}", file=sys.stderr)
        return 2
    a = [
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
        str(key),
        "--",
        f"student@{n['address']}",
    ]
    failed = False
    for desc, cmd, kind in CHECKS:
        try:
            r = subprocess.run([*a, cmd], text=True, capture_output=True, check=False, timeout=15)
            ok = r.returncode == 0 and valid(kind, r.stdout)
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        print(f"{'PASS' if ok else 'FAIL'} {LAB_ID}: {desc}")
        failed |= not ok
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
