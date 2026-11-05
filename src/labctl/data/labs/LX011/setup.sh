#!/usr/bin/bash
set -euo pipefail
marker=/var/lib/labctl/lx011-prepared
test ! -e "$marker" || exit 0
install -d -m 0755 /var/lib/labctl
install -d -o student -g student -m 0755 /home/student
install -d -m 0755 /srv/lx011
printf '%s\n' 'INFO boot' 'ERROR disk' 'WARN load' 'ERROR network' 'INFO ready' 'WARN memory' > /srv/lx011/events.log
chmod 0444 /srv/lx011/events.log
touch "$marker"
