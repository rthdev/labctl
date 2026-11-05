#!/bin/bash
set -euo pipefail
install -d -m 0700 /var/lib/labctl
# Setup is repeat-safe; never restage faults over an existing learner attempt.
test ! -e /var/lib/labctl/lx212-staged || exit 0
cat /proc/sys/kernel/random/boot_id > /var/lib/labctl/lx212-boot
dnf -y install bash coreutils
install -d -m 0755 /var/lib/lx212
printf '%s\n' 'ERROR disk' 'INFO boot' 'WARN space' 'ERROR retry' > /var/lib/lx212/events
cat > /usr/local/sbin/lx212-report <<'SCRIPT'
#!/bin/bash
echo 'report implementation missing' >&2
exit 2
SCRIPT
chmod 0755 /usr/local/sbin/lx212-report
cat > /etc/systemd/system/lx212-report.service <<'UNIT'
[Unit]
Description=Event report generation
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/lx212-reprot /var/lib/lx212/events /var/lib/lx212/report
UNIT
cat > /etc/systemd/system/lx212-report.timer <<'UNIT'
[Unit]
Description=Periodic event summary
[Timer]
OnBootSec=10s
OnUnitActiveSec=10s
AccuracySec=1s
Unit=lx212-report.service
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl disable --now lx212-report.timer

touch /var/lib/labctl/lx212-staged
