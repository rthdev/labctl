#!/bin/sh
set -eu
install -d -m 0755 /var/tmp/lx202-cache
printf stale > /var/tmp/lx202-cache/stale.tmp
rm -f /etc/systemd/system/lx202-maintenance.service /etc/systemd/system/lx202-maintenance.timer /usr/local/sbin/lx202-maintenance /var/log/lx202-maintenance.log
systemctl daemon-reload
