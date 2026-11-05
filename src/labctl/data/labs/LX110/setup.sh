#!/usr/bin/bash
set -euo pipefail
marker=/var/lib/labctl/lx110-prepared
test ! -e "$marker" || exit 0
install -d -m 0755 /var/lib/labctl
install -d -o student -g student -m 0755 /home/student
:
touch "$marker"
