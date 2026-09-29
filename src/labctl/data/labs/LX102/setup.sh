#!/bin/sh
set -eu
dnf -y install xfsprogs
install -d -m 0755 /var/lib/labctl-media /srv/archive
if [ ! -f /var/lib/labctl-media/lx102-storage.img ]; then
  truncate -s 768M /var/lib/labctl-media/lx102-storage.img
  mkfs.xfs -f /var/lib/labctl-media/lx102-storage.img
fi
umount /srv/archive >/dev/null 2>&1 || true
sed -i '\|/var/lib/labctl-media/lx102-storage.img|d' /etc/fstab
rm -f /srv/archive/persistent.txt
