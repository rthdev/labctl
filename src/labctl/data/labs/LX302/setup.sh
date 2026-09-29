#!/bin/sh
set -eu
dnf -y install lvm2 xfsprogs
install -d -m 0755 /var/lib/labctl-media /srv/lvmdata
install -d -m 0700 /var/lib/labctl
for name in pv1 pv2; do [ -f /var/lib/labctl-media/lx302-$name.img ] || truncate -s 512M /var/lib/labctl-media/lx302-$name.img; done
if ! /usr/sbin/vgs vgdata >/dev/null 2>&1; then
  dev=$(losetup --find --show /var/lib/labctl-media/lx302-pv1.img)
  pvcreate -ff -y "$dev"
  vgcreate vgdata "$dev"
  lvcreate -L 300M -n lvdata vgdata
  mkfs.xfs -f /dev/vgdata/lvdata
fi
mountpoint -q /srv/lvmdata || mount /dev/vgdata/lvdata /srv/lvmdata
rm -f /srv/lvmdata/recovery.txt
cat /proc/sys/kernel/random/boot_id > /var/lib/labctl/lx302-boot-baseline
chmod 0600 /var/lib/labctl/lx302-boot-baseline
