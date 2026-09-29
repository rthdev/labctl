#!/bin/bash
set -euo pipefail
dnf -y install podman shadow-utils python3 slirp4netns fuse-overlayfs curl
# Never overlap another account's subordinate IDs or replace an existing mapping.
python3 - <<'CT_SUBIDS'
from pathlib import Path
import subprocess

def ensure_subids(path, option):
    records = []
    for line in Path(path).read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            user, start, count = line.split(":")
            start, count = int(start), int(count)
            if start < 1 or count < 1:
                raise RuntimeError("invalid subordinate ID range")
            records.append((user, start, start + count))
    own = [(lo, hi) for user, lo, hi in records if user == "student"]
    if own:
        if not any(hi - lo >= 65536 for lo, hi in own):
            raise RuntimeError("student needs a contiguous 65536 subordinate IDs")
        if any(lo < other_hi and other_lo < hi for lo, hi in own
               for user, other_lo, other_hi in records if user != "student"):
            raise RuntimeError("overlapping subordinate IDs; administrator repair required")
        return
    start = max([100000] + [hi for _, _, hi in records])
    subprocess.run(["/usr/sbin/usermod", option, f"{start}-{start + 65535}", "student"], check=True)

ensure_subids("/etc/subuid", "--add-subuids")
ensure_subids("/etc/subgid", "--add-subgids")
CT_SUBIDS
loginctl enable-linger student
uid=$(id -u student)
systemctl start "user@$uid.service"; install -d -m 0700 -o student -g student "/run/user/$uid" /home/student/.config /home/student/.config/containers /home/student/.config/containers/systemd
runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman image exists docker.io/library/alpine:3.20 || runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman pull docker.io/library/alpine:3.20
install -d -m 0755 /var/lib/labctl
if [ ! -e /var/lib/labctl/ct401-boot-baseline ]; then cat /proc/sys/kernel/random/boot_id > /var/lib/labctl/ct401-boot-baseline; fi
