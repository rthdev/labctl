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
systemctl start "user@$uid.service"; install -d -m 0700 -o student -g student "/run/user/$uid" /home/student/.config /home/student/.config/containers; install -d -m 0755 -o student -g student /home/student/ct301
runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman image exists docker.io/library/alpine:3.20 || runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman pull docker.io/library/alpine:3.20
if [ ! -e /home/student/LAB.md ]; then cat > /home/student/LAB.md <<'EOF'
# CT301: Pod and configuration

Write `/home/student/ct301/app.conf` as exactly `mode=training` plus newline.
Create pod `ct301-stack` and running Alpine containers `ct301-web` and
`ct301-checker` in it. `ct301-web` needs `APP_MODE=training` and a read-only bind
of that file to `/etc/ct301/app.conf`; serve it as `/config` on pod-local 8080.
The checker must be able to request `http://127.0.0.1:8080/config`.

The directory starts empty. Do not publish ports, use sudo/privileged Podman,
copy config into the image, or contact a registry. The preloaded tag is mutable.
Use a shared pod network namespace (the normal Podman pod default). An optional
infra container is allowed. Keep SELinux enabled; use `:ro,Z` for this private
configuration bind. Alpine's BusyBox `httpd -f -p 8080 -h /srv` can serve a
`/srv/config` symlink pointing to `/etc/ct301/app.conf`.

Use the preloaded BusyBox `httpd` in `ct301-web`, not a checker-hosted server.
Its live document root must resolve `/config` to the mounted file itself, not
an independent copy or constant response. Keep the host backing path free of
symlinks and do not overlay the configuration bind or any of its parent paths.
For this read-only grading probe, run `httpd` (or `busybox httpd`) with only
separate `-f`, `-v`, `-vv`, `-p PORT`, and/or `-h DIRECTORY` arguments; the document
root may vary. Do not add `httpd.conf`, redirects, CGI, or custom server options.
The grader correlates the web process's listening socket and live document root
with the original file's identity and exact bytes; it never edits configuration.

## Common environment and boundaries
One Rocky Linux 9 VM named `node` is supplied. Work as `student` using rootless
Podman and `docker.io/library/alpine:3.20` (preloaded in your image store).
Use `--pull=never` for offline runs. Package/image acquisition during setup needs
Internet; the upstream image tag is mutable, not digest-pinned. No host container
engine is used. Keep SELinux confinement intact; never use privileged containers,
rootful Podman, disabled labels, host networking, or host-service substitutes.
No guest reboot is required for this lab. Leave all requested evidence in place.
From the labctl host, run `labctl lab grade CT301` to check your work.
EOF
chown student:student /home/student/LAB.md; fi
