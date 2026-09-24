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
systemctl start "user@$uid.service"; install -d -m 0700 -o student -g student "/run/user/$uid" /home/student/.config/containers /home/student/.config/containers/systemd
runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman image exists docker.io/library/alpine:3.20 || runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman pull docker.io/library/alpine:3.20
install -d -m 0755 /var/lib/labctl
if [ ! -e /var/lib/labctl/ct401-boot-baseline ]; then cat /proc/sys/kernel/random/boot_id > /var/lib/labctl/ct401-boot-baseline; fi
if [ ! -e /home/student/LAB.md ]; then cat > /home/student/LAB.md <<'EOF'
# CT401: Rootless Quadlet persistence

Create `~/.config/containers/systemd/ct401-web.container` for `student`. It must
generate `ct401-web.service`, use `docker.io/library/alpine:3.20`, set
`ContainerName=ct401-web`, and run a process that keeps the container alive with
`/srv/status.txt` containing exactly `CT401 persistent service` plus newline.
Use `[Container]` with `Image=docker.io/library/alpine:3.20`, `Pull=never`,
`ContainerName=ct401-web`, and a suitable `Exec=` command. Include:
```ini
[Install]
WantedBy=default.target
```
Then use `systemctl --user daemon-reload` and `systemctl --user start ct401-web.service`.
Quadlet-generated units are not enabled with `systemctl enable`: the generator
applies the `[Install]` dependency at each boot. Reboot the VM with `sudo reboot`.
Leave the generated service active automatically, not manually started after reboot.
Linger must remain enabled. The grader ties the service's MainPID to the container's
ConmonPid and validates its source, generated path, and boot target dependency.

The baseline boot ID is recorded before your work and linger is enabled. No
objective Quadlet/container exists. Do not use a root/system service, generated
unit copied by hand, sudo/privileged Podman, restart-policy substitute, or registry.
The mutable image tag was downloaded during setup, which requires Internet.
## Common environment and boundaries
One Rocky Linux 9 VM named `node` is supplied. Work as `student` using rootless
Podman and `docker.io/library/alpine:3.20` (preloaded in your image store).
Use `--pull=never` for offline runs. Package/image acquisition during setup needs
Internet; the upstream image tag is mutable, not digest-pinned. No host container
engine is used. Keep SELinux confinement intact; never use privileged containers,
rootful Podman, disabled labels, host networking, or host-service substitutes.
From the labctl host, run `labctl lab grade CT401` to check your work.
EOF
chown student:student /home/student/LAB.md; fi
