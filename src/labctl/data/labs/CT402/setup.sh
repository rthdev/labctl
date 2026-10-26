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
systemctl start "user@$uid.service"; install -d -m 0700 -o student -g student "/run/user/$uid" /home/student/.config /home/student/.config/containers; install -d -m 0755 /var/lib/labctl
install -d -m 0755 /var/lib/labctl
# Refresh mutable minor tags on fresh provisioning, never during grading.
if [ ! -s /var/lib/labctl/ct-image-provenance ]; then
 : > /var/lib/labctl/ct-image-provenance.new
 runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman pull registry.access.redhat.com/ubi9/ubi:9.8
 runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman image inspect --format '{{.Id}} {{json .RepoDigests}}' registry.access.redhat.com/ubi9/ubi:9.8 >> /var/lib/labctl/ct-image-provenance.new
 runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman pull registry.access.redhat.com/ubi9/httpd-24:9.8
 runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman image inspect --format '{{.Id}} {{json .RepoDigests}}' registry.access.redhat.com/ubi9/httpd-24:9.8 >> /var/lib/labctl/ct-image-provenance.new
 mv /var/lib/labctl/ct-image-provenance.new /var/lib/labctl/ct-image-provenance
fi
student_podman() {
 runuser -u student -- env HOME=/home/student USER=student LOGNAME=student XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" /usr/bin/podman "$@"
}
if [ ! -e /var/lib/labctl/ct402-initialized ]; then
 cat > /home/student/ct-httpd.conf <<'CT_HTTPD'
ServerRoot "/etc/httpd"
LoadModule mpm_event_module modules/mod_mpm_event.so
LoadModule unixd_module modules/mod_unixd.so
LoadModule authz_core_module modules/mod_authz_core.so
Listen 9090
ServerName localhost
PidFile /tmp/ct-httpd.pid
ErrorLog /dev/stderr
DocumentRoot "/srv"
<Directory "/srv">
    Options FollowSymLinks
    AllowOverride None
    Require all granted
</Directory>
CT_HTTPD
 chown student:student /home/student/ct-httpd.conf
 chmod 0644 /home/student/ct-httpd.conf
 if ! student_podman volume exists ct402-data; then
  student_podman volume create ct402-data >/dev/null
 fi
 if [ ! -s /var/lib/labctl/ct402-volume-baseline ]; then
  student_podman volume inspect ct402-data > /var/lib/labctl/ct402-volume-baseline.new
  mv /var/lib/labctl/ct402-volume-baseline.new /var/lib/labctl/ct402-volume-baseline
 fi
 student_podman run --rm --pull=never --network=none -v ct402-data:/srv:Z registry.access.redhat.com/ubi9/ubi:9.8 sh -ec "test -e /srv/health || printf 'CT402 recovered API\n' > /srv/health"
 if ! student_podman container exists ct402-api; then
  student_podman run -d --pull=never --name ct402-api -e API_PORT=9090 -p 127.0.0.1:8080:8080 -v ct402-data:/srv:Z -v /home/student/ct-httpd.conf:/tmp/ct-httpd.conf:ro,Z registry.access.redhat.com/ubi9/httpd-24:9.8 /usr/sbin/httpd -f /tmp/ct-httpd.conf -DFOREGROUND >/dev/null
 fi
 # Do not declare the incident staged unless the API itself is healthy and
 # the deliberately mismatched published target cannot answer.
 healthy=false
 for attempt in {1..30}; do
  if [ "$(student_podman exec ct402-api curl --noproxy '*' --max-time 2 -fsS http://127.0.0.1:9090/health)" = 'CT402 recovered API' ]; then
   healthy=true; break
  fi
  sleep 1
 done
 "$healthy" || { printf '%s\n' 'CT402 internal API failed readiness' >&2; exit 1; }
 if curl --noproxy '*' --max-time 3 -fsS http://127.0.0.1:8080/health; then
  printf '%s\n' 'CT402 expected published-port failure was not staged' >&2; exit 1
 fi
 printf '%s\n' 'staged-api-port=9090' 'published-target=8080' > /var/lib/labctl/ct402-fault-baseline
 touch /var/lib/labctl/ct402-initialized
fi
