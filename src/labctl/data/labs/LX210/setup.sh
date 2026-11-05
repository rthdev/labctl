#!/bin/bash
set -euo pipefail
install -d -m 0700 /var/lib/labctl
# Setup is repeat-safe; never restage faults over an existing learner attempt.
test ! -e /var/lib/labctl/lx210-staged || exit 0
cat /proc/sys/kernel/random/boot_id > /var/lib/labctl/lx210-boot
dnf -y install lvm2 xfsprogs
install -d -m 0755 /var/lib/labctl-media
truncate -s 768M /var/lib/labctl-media/lx210.img
cat > /usr/local/sbin/lx210-attach <<'SCRIPT'
#!/bin/bash
set -euo pipefail
image=/var/lib/labctl-media/lx210.img
device=$(losetup -j "$image" --output NAME --noheadings)
if test -z "$device"; then device=$(losetup --find --show "$image"); fi
# Explicit device selection also works with the EL10 LVM devices file enabled.
if pvs --devices "$device" "$device" >/dev/null 2>&1; then
    vgchange --devices "$device" -ay
fi
SCRIPT
chmod 0755 /usr/local/sbin/lx210-attach
cat > /etc/systemd/system/lx210-attach.service <<'UNIT'
[Unit]
Description=Attach supplied LX210 media before filesystem mounts
DefaultDependencies=no
After=systemd-udev-settle.service
Wants=systemd-udev-settle.service local-fs-pre.target
Before=local-fs-pre.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/lx210-attach
RemainAfterExit=yes
[Install]
WantedBy=sysinit.target
UNIT
systemctl daemon-reload
systemctl enable --now lx210-attach.service

touch /var/lib/labctl/lx210-staged
