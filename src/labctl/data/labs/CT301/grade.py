#!/usr/bin/python3
# ruff: noqa: S603
from __future__ import annotations

import ipaddress
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

LAB_ID = "CT301"
SERVICE_COMMAND = "/usr/bin/python3 -c " + shlex.quote(
    Path(__file__).with_name("service_probe.py").read_text()
    + "\nmain('ct301-web', '/etc/ct301/app.conf', '/home/student/ct301/app.conf', "
    + "'config', 'mode=training\\n')\n"
)
CHECKS = (
    ("web listener serves the original mounted config", SERVICE_COMMAND, "service"),
    ("mounted config is exact", "/usr/bin/podman exec ct301-web cat /etc/ct301/app.conf", "config"),
    ("rootless SELinux Podman store", "/usr/bin/podman info --format json", "info"),
    (
        "pod contains exactly the application containers",
        "/usr/bin/podman pod inspect ct301-stack",
        "pod",
    ),
    (
        "containers have exact configuration",
        "/usr/bin/podman inspect ct301-web ct301-checker",
        "containers",
    ),
    (
        "checker reaches pod-local configured service",
        "/usr/bin/podman exec ct301-checker wget -qO- http://127.0.0.1:8080/config",
        "probe",
    ),
)


def valid(k: str, t: str) -> bool:
    try:
        if k == "service":
            return t == "connected\n"
        if k == "config":
            return t == "mode=training\n"
        if k == "probe":
            return t.strip() == "mode=training"
        d = json.loads(t)
        if k == "info":
            return (
                d["host"]["security"]["rootless"] is True
                and d["host"]["security"]["selinuxEnabled"] is True
                and d["store"]["graphRoot"] == "/home/student/.local/share/containers/storage"
            )
        if k == "pod":
            if not isinstance(d, list) or len(d) != 1:
                return False
            d = d[0]
            return (
                d["Name"] == "ct301-stack"
                and d["State"].lower() == "running"
                and {
                    x["Name"]
                    for x in d["Containers"]
                    if x.get("Id") != d.get("InfraContainerID", "")
                }
                == {"ct301-web", "ct301-checker"}
            )
        by = {x["Name"]: x for x in d}
        web, checker = by["ct301-web"], by["ct301-checker"]
        mounts = [
            m
            for m in web["Mounts"]
            if m["Destination"] == "/etc/ct301/app.conf"
            or "/etc/ct301/app.conf".startswith(m["Destination"].rstrip("/") + "/")
            or m["Destination"].startswith("/etc/ct301/app.conf/")
        ]
        return (
            len(d) == 2
            and all(
                x["State"]["Status"] == "running"
                and x["Config"]["Image"] == "docker.io/library/alpine:3.20"
                and x["HostConfig"]["Privileged"] is False
                and ":container_t:" in x["ProcessLabel"]
                and not x["HostConfig"].get("PortBindings")
                for x in (web, checker)
            )
            and "APP_MODE=training" in web["Config"]["Env"]
            and len(mounts) == 1
            and mounts[0]["Destination"] == "/etc/ct301/app.conf"
            and mounts[0]["Type"] == "bind"
            and mounts[0]["Source"] == "/home/student/ct301/app.conf"
            and mounts[0]["RW"] is False
        )
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        return False


def connected(evidence: dict[str, str]) -> bool:
    """Tie pod membership to container IDs, not just display names."""
    try:
        pod = json.loads(evidence["pod"])[0]
        containers = json.loads(evidence["containers"])
        members = {x["Id"] for x in pod["Containers"] if x["Id"] != pod.get("InfraContainerID")}
        return (
            bool(pod["Id"])
            and members == {x["Id"] for x in containers}
            and all(x["Pod"] == pod["Id"] for x in containers)
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
