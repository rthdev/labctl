#!/usr/bin/bash
set -euo pipefail
marker=/var/lib/labctl/lx010-prepared
test ! -e "$marker" || exit 0
install -d -m 0755 /var/lib/labctl
install -d -o student -g student -m 0755 /home/student
install -d -m 0755 /srv/lx010
printf 'Welcome to Rocky 10\n' > /srv/lx010/welcome.txt
chmod 0444 /srv/lx010/welcome.txt
touch "$marker"
