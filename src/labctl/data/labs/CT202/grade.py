#!/usr/bin/python3
# ruff: noqa: S603
from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "CT202"
CHECKS = (
    ("rootless SELinux Podman store", "/usr/bin/podman info --format json", "info"),
    (
        "API and client share only the private network",
        "/usr/bin/podman inspect ct202-api ct202-client",
        "containers",
    ),
    ("network is internal", "/usr/bin/podman network inspect ct202-private", "network"),
    (
        "client reaches API by container DNS",
        "/usr/bin/podman exec ct202-client wget -qO- http://api:8080/health",
        "probe",
    ),
)


def valid(k: str, t: str) -> bool:
    try:
        if k == "probe":
            return t.strip() == "CT202 private API"
        d = json.loads(t)
        if k == "info":
            return (
                d["host"]["security"]["rootless"] is True
                and d["host"]["security"]["selinuxEnabled"] is True
                and d["store"]["graphRoot"] == "/home/student/.local/share/containers/storage"
            )
        if k == "network":
            return len(d) == 1 and d[0]["name"] == "ct202-private" and d[0]["internal"] is True
        by = {x["Name"]: x for x in d}
        api, client = by["ct202-api"], by["ct202-client"]
        return (
            len(d) == 2
            and all(
                x["State"]["Status"] == "running"
                and x["Config"]["Image"] == "docker.io/library/alpine:3.20"
                and x["HostConfig"]["Privileged"] is False
                and ":container_t:" in x["ProcessLabel"]
                and not x["HostConfig"].get("PortBindings")
                and set(x["NetworkSettings"]["Networks"]) == {"ct202-private"}
                for x in (api, client)
            )
            and "api" in api["NetworkSettings"]["Networks"]["ct202-private"]["Aliases"]
        )
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return False


def connected(evidence: dict[str, str]) -> bool:
    """Verify both endpoints actually attach to the inspected internal network."""
    try:
        network = json.loads(evidence["network"])[0]
        containers = json.loads(evidence["containers"])
        return bool(network["id"]) and all(
            x["NetworkSettings"]["Networks"]["ct202-private"]["NetworkID"] == network["id"]
            for x in containers
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
    evidence: dict[str, str] = {}
    for desc, cmd, k in CHECKS:
        try:
            r = subprocess.run([*a, cmd], text=True, capture_output=True, check=False, timeout=15)
            ok = r.returncode == 0 and valid(k, r.stdout)
            if ok:
                evidence[k] = r.stdout
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        print(f"{'PASS' if ok else 'FAIL'} {LAB_ID}: {desc}")
        bad |= not ok
    linked = connected(evidence)
    print(f"{'PASS' if linked else 'FAIL'} {LAB_ID}: connected runtime ownership")
    return int(bad or not linked)


if __name__ == "__main__":
    raise SystemExit(main())
