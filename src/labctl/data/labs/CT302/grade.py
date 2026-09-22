#!/usr/bin/python3
# ruff: noqa: E501, S603
from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "CT302"
CHECKS = (
    ("rootless SELinux Podman store", "/usr/bin/podman info --format json", "info"),
    (
        "declared controls and resources are exact",
        "/usr/bin/podman inspect ct302-secure",
        "inspect",
    ),
    (
        "identity and hardening are effective",
        '/usr/bin/podman exec ct302-secure sh -ec \'id -u; id -g; awk \'"\'"\'$2 == "/" {n=split($4,a,","); for(i=1;i<=n;i++) if(a[i]=="ro") print "ro"}\'"\'"\' /proc/mounts; grep -E \'"\'"\'^(NoNewPrivs|CapEff|CapBnd):\'"\'"\' /proc/1/status; cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/pids.max /sys/fs/cgroup/cpu.max\'',
        "probe",
    ),
)


def valid(k: str, t: str) -> bool:
    try:
        if k == "probe":
            lines = t.splitlines()
            if len(lines) != 9 or lines[:3] != ["10001", "10001", "ro"]:
                return False
            status = dict(line.split(":", 1) for line in lines[3:6])
            quota, period = (int(value) for value in lines[8].split())
            return (
                status["NoNewPrivs"].strip() == "1"
                and int(status["CapEff"], 16) == 0
                and int(status["CapBnd"], 16) == 0
                and lines[6:8] == ["268435456", "64"]
                and period > 0
                and quota * 2 == period
            )
        d = json.loads(t)
        if k == "info":
            return (
                d["host"]["security"]["rootless"] is True
                and d["host"]["security"]["selinuxEnabled"] is True
                and d["store"]["graphRoot"] == "/home/student/.local/share/containers/storage"
            )
        x = d[0]
        h = x["HostConfig"]
        return (
            len(d) == 1
            and x["Name"] == "ct302-secure"
            and x["State"]["Status"] == "running"
            and x["Config"]["Image"] == "docker.io/library/alpine:3.20"
            and x["Config"]["User"] == "10001:10001"
            and h["Privileged"] is False
            and h["ReadonlyRootfs"] is True
            and h["Memory"] == 268435456
            and (
                h.get("NanoCpus") == 500000000
                or (h.get("CpuPeriod", 0) > 0 and h.get("CpuQuota", -1) * 2 == h["CpuPeriod"])
            )
            and h["PidsLimit"] == 64
            and x["EffectiveCaps"] == []
            and x["BoundingCaps"] == []
            and "no-new-privileges" in h["SecurityOpt"]
            and ":container_t:" in x["ProcessLabel"]
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
