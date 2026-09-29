#!/bin/sh
set -eu
dnf -y install xfsprogs
install -d -m 0700 /var/lib/labctl
install -d -m 0755 /var/lib/labctl-media /srv/recovery
[ -f /var/lib/labctl-media/lx401-recovery.img ] || { truncate -s 512M /var/lib/labctl-media/lx401-recovery.img; mkfs.xfs -f /var/lib/labctl-media/lx401-recovery.img; }
getent shadow root | cut -d: -f2 > /var/lib/labctl/lx401-root-baseline
cat /proc/sys/kernel/random/boot_id > /var/lib/labctl/lx401-boot-baseline
chmod 0600 /var/lib/labctl/lx401-root-baseline
chmod 0600 /var/lib/labctl/lx401-boot-baseline
passwd -l root >/dev/null
sed -i '\|/srv/recovery|d' /etc/fstab
printf '%s\n' 'UUID=00000000-0000-0000-0000-000000000000 /srv/recovery xfs defaults 0 2' >> /etc/fstab
if ! grep -q 'audit=O' /etc/default/grub; then sed -i 's/GRUB_CMDLINE_LINUX="/GRUB_CMDLINE_LINUX="audit=O /' /etc/default/grub; fi
/usr/sbin/grubby --update-kernel=ALL --args='audit=O'
systemctl set-default multi-user.target
# Stage relabel evidence last so setup cannot repair it before the learner reboots.
chcon -t user_tmp_t /etc/shadow
touch /.autorelabel
