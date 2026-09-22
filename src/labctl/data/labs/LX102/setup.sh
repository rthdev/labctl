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
cat > /home/student/LAB.md <<'EOF'
# LX102: Persistent storage and mounts
The 768 MiB loop-backed image at `/var/lib/labctl-media/lx102-storage.img` is the
lab's storage medium (schema v1 has no extra-disk field). Mount its XFS filesystem
at `/srv/archive`, add a valid persistent `loop,nofail` fstab entry, and create
`persistent.txt` containing exactly `LX102 persistent storage`.
EOF
chown student:student /home/student/LAB.md
