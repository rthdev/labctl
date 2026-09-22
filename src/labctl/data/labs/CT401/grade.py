#!/usr/bin/python3
# ruff: noqa: E501, S603
from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

LAB_ID = "CT401"
CHECKS = (
    ("rootless SELinux Podman store", "/usr/bin/podman info --format json", "info"),
    ("Quadlet container is running", "/usr/bin/podman inspect ct401-web", "inspect"),
    (
        "VM rebooted after setup",
        "/usr/bin/cat /var/lib/labctl/ct401-boot-baseline /proc/sys/kernel/random/boot_id",
        "reboot",
    ),
    ("linger is enabled", "/usr/bin/loginctl show-user student -p Linger --value", "linger"),
    (
        "generated unit owns its monitor",
        "/usr/bin/systemctl --user show ct401-web.service -p ActiveState -p MainPID -p SourcePath -p FragmentPath",
        "unit",
    ),
    (
        "default target starts the service",
        "/usr/bin/systemctl --user show default.target -p Wants --value",
        "wants",
    ),
    (
        "persistent payload is exact",
        "/usr/bin/podman exec ct401-web cat /srv/status.txt",
        "payload",
    ),
)


def properties(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def valid(k: str, t: str) -> bool:
    try:
        if k == "payload":
            return t.strip() == "CT401 persistent service"
        if k == "linger":
            return t.strip() == "yes"
        if k == "wants":
            return "ct401-web.service" in t.split()
        if k == "reboot":
            ids = t.splitlines()
            return len(ids) == 2 and all(str(uuid.UUID(x)) == x for x in ids) and ids[0] != ids[1]
        if k == "unit":
            unit = properties(t)
            fragment = Path(unit["FragmentPath"])
            return (
                unit["ActiveState"] == "active"
                and int(unit["MainPID"]) > 0
                and unit["SourcePath"]
                == "/home/student/.config/containers/systemd/ct401-web.container"
                and fragment.name == "ct401-web.service"
                and fragment.parent.name in {"generator", "generator.early", "generator.late"}
                and str(fragment).startswith("/run/user/")
            )
        d = json.loads(t)
        if k == "info":
            return (
                d["host"]["security"]["rootless"] is True
                and d["host"]["security"]["selinuxEnabled"] is True
                and d["store"]["graphRoot"] == "/home/student/.local/share/containers/storage"
            )
        x = d[0]
        return (
            len(d) == 1
            and x["Name"] == "ct401-web"
            and x["State"]["Status"] == "running"
            and x["State"]["ConmonPid"] > 0
            and x["Config"]["Image"] == "docker.io/library/alpine:3.20"
            and x["HostConfig"]["Privileged"] is False
            and ":container_t:" in x["ProcessLabel"]
            and x["HostConfig"]["RestartPolicy"]["Name"] in {"", "no"}
        )
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        return False


def connected(evidence: dict[str, str]) -> bool:
    """Quadlet's MainPID is the monitor of this exact container."""
    try:
        item = json.loads(evidence["inspect"])[0]
        return bool(item["State"]["ConmonPid"] == int(properties(evidence["unit"])["MainPID"]))
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
    print(f"{'PASS' if linked else 'FAIL'} {LAB_ID}: service owns the running container")
    return int(bad or not linked)


if __name__ == "__main__":
    raise SystemExit(main())
