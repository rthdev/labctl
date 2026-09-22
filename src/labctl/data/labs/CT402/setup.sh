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
systemctl start "user@$uid.service"; install -d -m 0700 -o student -g student "/run/user/$uid" /home/student/.config/containers; install -d -m 0755 /var/lib/labctl
runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman image exists docker.io/library/alpine:3.20 || runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman pull docker.io/library/alpine:3.20
student_podman() {
 runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman "$@"
}
if [ ! -e /var/lib/labctl/ct402-initialized ]; then
 if ! student_podman volume exists ct402-data; then
  student_podman volume create ct402-data >/dev/null
 fi
 if [ ! -s /var/lib/labctl/ct402-volume-baseline ]; then
  student_podman volume inspect ct402-data > /var/lib/labctl/ct402-volume-baseline.new
  mv /var/lib/labctl/ct402-volume-baseline.new /var/lib/labctl/ct402-volume-baseline
 fi
 student_podman run --rm --pull=never --network=none -v ct402-data:/srv:Z docker.io/library/alpine:3.20 sh -ec "test -e /srv/health || printf 'CT402 recovered API\n' > /srv/health"
 if ! student_podman container exists ct402-api; then
  student_podman run -d --pull=never --name ct402-api -e API_PORT=9090 -p 127.0.0.1:8080:8080 -v ct402-data:/srv:Z docker.io/library/alpine:3.20 sh -c 'exec httpd -f -p "$API_PORT" -h /srv' >/dev/null
 fi
 printf '%s\n' 'staged-api-port=9090' 'published-target=8080' > /var/lib/labctl/ct402-fault-baseline
 touch /var/lib/labctl/ct402-initialized
fi
if [ ! -e /home/student/LAB.md ]; then cat > /home/student/LAB.md <<'EOF'
# CT402: Incident recovery capstone

`ct402-api` publishes host `127.0.0.1:8080` to container port 8080, but its API
is configured for another port. Use inspect and logs to identify the mismatch.
Preserve named volume `ct402-data`; recreate the same named rootless Alpine
container with `API_PORT=8080`, the same volume at `/srv`, and the exact loopback
mapping. Leave it running. `/health` must return `CT402 recovered API`.

Create `/home/student/ct402-incident-report` with exactly:
`root cause: API_PORT was 9090 while published target was 8080`
`recovery: recreated ct402-api on 8080 with ct402-data preserved`

Do not erase the volume, edit the baseline, run a host server, use sudo or
privileged Podman, disable SELinux, or contact a registry. Setup stages the fault
only once, so reruns preserve recovery. The preloaded upstream tag is mutable.
Preserve the original volume object as well as its data: its creation timestamp
is recorded by setup. Recovery is not complete if the volume was deleted and
replaced, even with matching text. Confirm the guest's loopback endpoint with
`curl --noproxy '*' http://127.0.0.1:8080/health` as well as a container-local probe.

Serve the original `/srv/health` file (exactly `CT402 recovered API` plus newline)
with the preloaded BusyBox `httpd` in `ct402-api`. Do not replace that backing file
with a symlink, overlay `/srv` or its contents, or serve an independent copy.
For this read-only grading probe, run `httpd` (or `busybox httpd`) with only
separate `-f`, `-v`, `-vv`, `-p PORT`, and/or `-h DIRECTORY` arguments; the document
root may vary if `/health` still resolves to the original volume file. Do not add
`httpd.conf`, redirects, CGI, or custom server options. The grader independently
reads the volume's data and correlates its identity with the effective mount,
server's live document root and listening socket; it never edits the volume.

## Common environment and boundaries
One Rocky Linux 9 VM named `node` is supplied. Work as `student` using rootless
Podman and `docker.io/library/alpine:3.20` (preloaded in your image store).
Use `--pull=never` for offline runs. Package/image acquisition during setup needs
Internet; the upstream image tag is mutable, not digest-pinned. No host container
engine is used. Keep SELinux confinement intact; never use privileged containers,
rootful Podman, disabled labels, host networking, or host-service substitutes.
No guest reboot is required for this lab. Leave all requested evidence in place.
From the labctl host, run `labctl lab grade CT402` to check your work.
EOF
chown student:student /home/student/LAB.md; fi
