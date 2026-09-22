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
cat > /home/student/LAB.md <<'EOF'
# LX302: LVM expansion and recovery
Two 512 MiB loop-backed images in `/var/lib/labctl-media` are the lab disks.
The first backs `vgdata/lvdata`; attach the second to a free loop device, add it
as a PV, and extend the LV and its XFS filesystem to at least 700 MiB. Preserve
the mount and write `LX302 recovered and expanded` to `recovery.txt`. Configure
the loop attachment durably in an enabled `lx302-loop-pvs.service`. The oneshot
must use `DefaultDependencies=no`, run before both `local-fs-pre.target` and
`lvm2-monitor.service`, attach both images and run `pvscan`, remain active, and
be installed under `sysinit.target`. Add a valid fstab mount, then reboot. The
volume must mount at `/srv/lvmdata` from fstab during that new boot.
EOF
chown student:student /home/student/LAB.md
