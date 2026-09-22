#!/usr/bin/python3
# ruff: noqa: E501, S603
from __future__ import annotations

import ipaddress
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

LAB_ID = "CT402"
SERVICE_COMMAND = "/usr/bin/python3 -c " + shlex.quote(
    Path(__file__).with_name("service_probe.py").read_text()
    + "\nmain('ct402-api', '/srv/health', 'ct402-data', "
    + "'health', 'CT402 recovered API\\n')\n"
)
CHECKS = (
    ("API serves the original volume data", SERVICE_COMMAND, "service"),
    ("mounted health data is exact", "/usr/bin/podman exec ct402-api cat /srv/health", "data"),
    ("volume is retained", "/usr/bin/podman volume inspect ct402-data", "volume"),
    (
        "original volume identity is recorded",
        "/usr/bin/cat /var/lib/labctl/ct402-volume-baseline",
        "baseline-volume",
    ),
    (
        "published loopback endpoint responds",
        "/usr/bin/curl --noproxy '*' --max-time 5 -fsS http://127.0.0.1:8080/health",
        "probe",
    ),
    ("rootless SELinux Podman store", "/usr/bin/podman info --format json", "info"),
    (
        "recovered API has exact port, volume, and runtime state",
        "/usr/bin/podman inspect ct402-api",
        "inspect",
    ),
    (
        "health is served by the recovered container",
        "/usr/bin/podman exec ct402-api wget -qO- http://127.0.0.1:8080/health",
        "probe",
    ),
    ("incident report is exact", "/usr/bin/cat /home/student/ct402-incident-report", "report"),
    (
        "staged fault evidence remains intact",
        "/usr/bin/cat /var/lib/labctl/ct402-fault-baseline",
        "baseline",
    ),
)


def valid(k: str, t: str) -> bool:
    try:
        if k == "service":
            return t == "connected\n"
        if k == "data":
            return t == "CT402 recovered API\n"
        expected = {
            "probe": "CT402 recovered API",
            "report": "root cause: API_PORT was 9090 while published target was 8080\nrecovery: recreated ct402-api on 8080 with ct402-data preserved",
            "baseline": "staged-api-port=9090\npublished-target=8080",
        }
        if k in expected:
            return t.strip() == expected[k]
        d = json.loads(t)
        if k in {"volume", "baseline-volume"}:
            return len(d) == 1 and d[0]["Name"] == "ct402-data" and bool(d[0]["CreatedAt"])
        if k == "info":
            return (
                d["host"]["security"]["rootless"] is True
                and d["host"]["security"]["selinuxEnabled"] is True
                and d["store"]["graphRoot"] == "/home/student/.local/share/containers/storage"
            )
        x = d[0]
        mounts = [
            m
            for m in x["Mounts"]
            if m["Destination"] == "/srv"
            or "/srv".startswith(m["Destination"].rstrip("/") + "/")
            or m["Destination"].startswith("/srv/")
        ]
        return (
            len(d) == 1
            and x["Name"] == "ct402-api"
            and x["State"]["Status"] == "running"
            and x["Config"]["Image"] == "docker.io/library/alpine:3.20"
            and "API_PORT=8080" in x["Config"]["Env"]
            and x["HostConfig"]["Privileged"] is False
            and ":container_t:" in x["ProcessLabel"]
            and x["HostConfig"]["PortBindings"]
            == {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}
            and len(mounts) == 1
            and mounts[0]["Destination"] == "/srv"
            and mounts[0]["Type"] == "volume"
            and mounts[0]["Name"] == "ct402-data"
        )
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        return False


def connected(evidence: dict[str, str]) -> bool:
    try:
        current = json.loads(evidence["volume"])[0]
        baseline = json.loads(evidence["baseline-volume"])[0]
        return bool(current["CreatedAt"] == baseline["CreatedAt"])
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
    print(f"{'PASS' if linked else 'FAIL'} {LAB_ID}: original data volume preserved")
    return int(bad or not linked)


if __name__ == "__main__":
    raise SystemExit(main())
