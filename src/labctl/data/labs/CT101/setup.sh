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
systemctl start "user@$uid.service"; install -d -m 0700 -o student -g student "/run/user/$uid" /home/student/.config/containers
runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman image exists docker.io/library/alpine:3.20 || runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman pull docker.io/library/alpine:3.20
if [ ! -e /home/student/LAB.md ]; then cat > /home/student/LAB.md <<'EOF'
# CT101: Rootless web ports

Create running `ct101-web` from `docker.io/library/alpine:3.20`. Serve
`CT101 rootless web` (newline allowed) at `/index.html` on TCP 8080 in the
container and publish exactly `127.0.0.1:8080:8080`.

No objective container exists initially. Use rootless Podman as `student` and
the preloaded mutable-tag image. Do not use sudo, privileged mode, a host web
server, or a wildcard host bind. Grading correlates inspect port state with a
request executed in `ct101-web`; it does not contact a registry.
Hint: Alpine includes BusyBox `httpd`. Its foreground form is
`httpd -f -p 8080 -h /srv`; create `/srv/index.html` before starting it.
The grader probes both the container-local endpoint and the host loopback port.

## Common environment and boundaries
One Rocky Linux 9 VM named `node` is supplied. Work as `student` using rootless
Podman and `docker.io/library/alpine:3.20` (preloaded in your image store).
Use `--pull=never` for offline runs. Package/image acquisition during setup needs
Internet; the upstream image tag is mutable, not digest-pinned. No host container
engine is used. Keep SELinux confinement intact; never use privileged containers,
rootful Podman, disabled labels, host networking, or host-service substitutes.
No guest reboot is required for this lab. Leave all requested evidence in place.
From the labctl host, run `labctl lab grade CT101` to check your work.
EOF
chown student:student /home/student/LAB.md; fi
