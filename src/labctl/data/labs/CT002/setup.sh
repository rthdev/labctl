#!/bin/bash
set -euo pipefail
dnf -y install podman shadow-utils python3 slirp4netns fuse-overlayfs curl-minimal
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
install -d -m 0700 -o student -g student "/run/user/$uid" /home/student/.config/containers
if ! runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman image exists docker.io/library/alpine:3.20; then
 runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman pull docker.io/library/alpine:3.20
fi
if [ ! -e /home/student/LAB.md ]; then
 cat > /home/student/LAB.md <<'EOF'
# CT002: Lifecycle, logs, and exec

Create `ct002-worker` from `docker.io/library/alpine:3.20`. Its initial command
must be `sh -c "echo 'CT002 worker ready'; exec sleep infinity"`. Stop, inspect,
and restart it. Leave it running. With `podman exec`, create `/lesson/status`
containing exactly `lifecycle inspected` followed by a newline.

## Guided practice
```sh
podman run --name ct002-worker --pull=never -d docker.io/library/alpine:3.20 sh -c "echo 'CT002 worker ready'; exec sleep infinity"
podman logs ct002-worker
podman stop ct002-worker
podman ps --all
podman start ct002-worker
podman exec ct002-worker sh -c 'mkdir -p /lesson; printf "lifecycle inspected\n" > /lesson/status'
podman exec ct002-worker cat /lesson/status
```
`-d` runs in the background. Stop/start keeps the writable container layer;
removing and recreating it would lose that layer. Repeated starts append log lines.

Podman and the mutable-tag image are preloaded. There is no objective container.
Use rootless Podman as `student`; no sudo, privileged container, host substitute,
or registry access is allowed. Grading reads inspect JSON, logs, and the file from
that same named container without modifying it.
## Common environment and boundaries
One Rocky Linux 9 VM named `node` is supplied. Work as `student` using rootless
Podman and `docker.io/library/alpine:3.20` (preloaded in your image store).
Use `--pull=never` for offline runs. Package/image acquisition during setup needs
Internet; the upstream image tag is mutable, not digest-pinned. No host container
engine is used. Keep SELinux confinement intact; never use privileged containers,
rootful Podman, disabled labels, host networking, or host-service substitutes.
No guest reboot is required for this lab. Leave all requested evidence in place.
From the labctl host, run `labctl lab grade CT002` to check your work.
EOF
 chown student:student /home/student/LAB.md
fi
