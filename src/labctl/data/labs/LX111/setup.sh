#!/usr/bin/bash
set -euo pipefail
marker=/var/lib/labctl/lx111-prepared
test ! -e "$marker" || exit 0
install -d -m 0755 /var/lib/labctl
install -d -o student -g student -m 0755 /home/student
dnf -y remove tree
cat > /etc/systemd/system/lx111-ready.service <<'UNIT'
[Unit]
Description=LX111 journal practice
[Service]
Type=oneshot
ExecStart=/usr/bin/echo LX111 service ready
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl disable --now lx111-ready.service
touch "$marker"
