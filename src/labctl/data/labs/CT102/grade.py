#!/usr/bin/python3
# ruff: noqa: S603
from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "CT102"
CHECKS = (
    ("rootless SELinux Podman store", "/usr/bin/podman info --format json", "info"),
    ("writer uses the named volume", "/usr/bin/podman inspect ct102-writer", "inspect"),
    (
        "volume data is exact in the same container",
        "/usr/bin/podman exec ct102-writer cat /data/message.txt",
        "probe",
    ),
    (
        "named volume belongs to the student store",
        "/usr/bin/podman volume inspect ct102-data",
        "volume",
    ),
)


def valid(k: str, t: str) -> bool:
    try:
        if k == "probe":
            return t.strip() == "CT102 durable data"
        d = json.loads(t)
        if k == "info":
            return (
                d["host"]["security"]["rootless"] is True
                and d["host"]["security"]["selinuxEnabled"] is True
                and d["store"]["graphRoot"] == "/home/student/.local/share/containers/storage"
            )
        x = d[0]
        if k == "volume":
            return (
                len(d) == 1
                and x["Name"] == "ct102-data"
                and x["Mountpoint"]
                == "/home/student/.local/share/containers/storage/volumes/ct102-data/_data"
            )
        mounts = [m for m in x["Mounts"] if m.get("Destination") == "/data"]
        return (
            len(d) == 1
            and x["Name"] == "ct102-writer"
            and x["State"]["Status"] == "running"
            and x["Config"]["Image"] == "docker.io/library/alpine:3.20"
            and x["HostConfig"]["Privileged"] is False
            and ":container_t:" in x["ProcessLabel"]
            and len(mounts) == 1
            and mounts[0]["Type"] == "volume"
            and mounts[0]["Name"] == "ct102-data"
        )
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        return False


def main() -> int:
    try:
        c = json.loads(Path(sys.argv[1]).read_text()) if len(sys.argv) == 2 else {}
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
    bad = False
    for desc, cmd, k in CHECKS:
        try:
            r = subprocess.run([*a, cmd], text=True, capture_output=True, check=False, timeout=15)
            ok = r.returncode == 0 and valid(k, r.stdout)
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        print(f"{'PASS' if ok else 'FAIL'} {LAB_ID}: {desc}")
        bad |= not ok
    return int(bad)


if __name__ == "__main__":
    raise SystemExit(main())
