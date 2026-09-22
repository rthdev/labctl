#!/usr/bin/python3
# ruff: noqa: S603
from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "CT201"
CHECKS = (
    ("application files originate in the image", "/usr/bin/podman diff ct201-app", "diff"),
    ("rootless SELinux Podman store", "/usr/bin/podman info --format json", "info"),
    (
        "container runs the locally built labeled image",
        "/usr/bin/podman inspect ct201-app",
        "container",
    ),
    (
        "local image has the exact tag and label",
        "/usr/bin/podman image inspect localhost/ct201-app:1",
        "image",
    ),
    (
        "built payload is present in the running container",
        "/usr/bin/podman exec ct201-app cat /app/result.txt",
        "probe",
    ),
)


def valid(k: str, t: str) -> bool:
    try:
        if k == "diff":
            for line in t.splitlines():
                if not line.strip():
                    continue
                operation, path = line.split(" ", 1)
                if operation not in {"A", "C", "D"} or not path.startswith("/"):
                    return False
                if path == "/app" or path.startswith("/app/"):
                    return False
            return True
        if k == "probe":
            return t.strip() == "CT201 image build"
        d = json.loads(t)
        if k == "info":
            return (
                d["host"]["security"]["rootless"] is True
                and d["host"]["security"]["selinuxEnabled"] is True
                and d["store"]["graphRoot"] == "/home/student/.local/share/containers/storage"
            )
        x = d[0]
        if k == "image":
            return (
                len(d) == 1
                and "localhost/ct201-app:1" in x["RepoTags"]
                and x["Labels"]["org.opencontainers.image.title"] == "CT201 app"
            )
        return (
            len(d) == 1
            and x["Name"] == "ct201-app"
            and x["State"]["Status"] == "running"
            and x["Config"]["Image"] == "localhost/ct201-app:1"
            and x["Config"]["Labels"]["org.opencontainers.image.title"] == "CT201 app"
            and x["HostConfig"]["Privileged"] is False
            and ":container_t:" in x["ProcessLabel"]
        )
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        return False


def connected(evidence: dict[str, str]) -> bool:
    """Bind the local image's immutable ID to the running workload."""
    try:
        container = json.loads(evidence["container"])[0]
        image = json.loads(evidence["image"])[0]
        return (
            bool(image["Id"])
            and container["Image"].removeprefix("sha256:") == image["Id"].removeprefix("sha256:")
            and not any(
                m["Destination"] == "/"
                or m["Destination"] == "/app"
                or m["Destination"].startswith("/app/")
                for m in container["Mounts"]
            )
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
