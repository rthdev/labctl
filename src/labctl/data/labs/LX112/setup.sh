#!/usr/bin/bash
set -euo pipefail
marker=/var/lib/labctl/lx112-prepared
test ! -e "$marker" || exit 0
install -d -m 0755 /var/lib/labctl
install -d -o student -g student -m 0755 /home/student
dnf -y install NetworkManager
systemctl enable --now NetworkManager
touch "$marker"
