#!/bin/sh
set -eu
install -d -m 0755 /var/tmp/lx202-cache
printf stale > /var/tmp/lx202-cache/stale.tmp
rm -f /etc/systemd/system/lx202-maintenance.service /etc/systemd/system/lx202-maintenance.timer /usr/local/sbin/lx202-maintenance /var/log/lx202-maintenance.log
systemctl daemon-reload
cat > /home/student/LAB.md <<'EOF'
# LX202: Scheduled jobs and system maintenance
Create an executable maintenance program that removes `stale.tmp` and writes
exactly `maintenance complete` to `/var/log/lx202-maintenance.log`. Run it from a
oneshot service and an enabled, active systemd timer scheduled daily at 02:15
with `Persistent=true`. Trigger the service once to provide completion evidence.
EOF
chown student:student /home/student/LAB.md
