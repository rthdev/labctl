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
systemctl start "user@$uid.service"
install -d -m 0700 -o student -g student "/run/user/$uid"
install -d -m 0700 -o student -g student \
    /home/student/.config /home/student/.config/containers
if ! runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman image exists docker.io/library/alpine:3.20; then
    runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman pull docker.io/library/alpine:3.20
fi
if [ ! -e /home/student/LAB.md ]; then
    cat > /home/student/LAB.md <<'EOF'
# CT001: Run your first rootless container

## Objective
As `student`, create `ct001-hello` from `docker.io/library/alpine:3.20` with the
exact command `printf 'CT001 container complete\n'`. Let it exit successfully and
retain it so `podman inspect` and `podman logs` still work.

## Guided first steps
Run these commands after `labctl lab ssh CT001 node`:
```sh
podman run --name ct001-hello --pull=never docker.io/library/alpine:3.20 printf 'CT001 container complete\n'
podman ps --all
podman logs ct001-hello
podman inspect ct001-hello
```
`--name` keeps an easy-to-find name; `--pull=never` uses the supplied image.
An exited container is normal: its one task finished. Do not use `--rm`, which
would delete the evidence. You can grade from the host with `labctl lab grade CT001`.

## Starting state
Podman and the image are installed, but no objective container is created.

## Rules
- Run Podman as `student`, never through `sudo` and never with `--privileged`.
- Do not replace Podman evidence with host files or processes.
- Do not remove `ct001-hello` after it exits.

The image tag is mutable upstream. Setup downloads it during provisioning; grading
uses only the local student image store and does not contact a registry.
## Common environment and boundaries
One Rocky Linux 9 VM named `node` is supplied. Work as `student` using rootless
Podman and `docker.io/library/alpine:3.20` (preloaded in your image store).
Use `--pull=never` for offline runs. Package/image acquisition during setup needs
Internet; the upstream image tag is mutable, not digest-pinned. No host container
engine is used. Keep SELinux confinement intact; never use privileged containers,
rootful Podman, disabled labels, host networking, or host-service substitutes.
No guest reboot is required for this lab. Leave all requested evidence in place.
From the labctl host, run `labctl lab grade CT001` to check your work.
EOF
    chown student:student /home/student/LAB.md
fi
