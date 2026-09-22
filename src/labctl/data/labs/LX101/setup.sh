#!/bin/sh
set -eu
dnf -y install curl
dnf -y remove httpd >/dev/null 2>&1 || true
rm -f /var/www/html/health /etc/systemd/journald.conf.d/lx101.conf
cat > /home/student/LAB.md <<'EOF'
# LX101: Packages, services, and logs
Install `httpd`, publish `LX101 healthy` at `/health`,
enable and start the service, configure persistent journald storage with a drop-in,
and demonstrate that the unit has journal entries. Use systemctl and journalctl.
EOF
chown student:student /home/student/LAB.md
