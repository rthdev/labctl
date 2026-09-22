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
cat > /home/student/LAB.md <<'EOF'
# LX401: Boot failure and root-password recovery
**Reboot the VM from labctl's console to begin.** SSH will stop when the staged
bad `audit=O` kernel argument (the last character is the letter O, not zero) and
fstab entry take effect. Use GRUB recovery/`rd.break` to
reset and unlock root. Keep SELinux enforcing and request a full relabel before
normal boot. A staged `/.autorelabel` marker and deliberately wrong `/etc/shadow`
label must be allowed to complete a full relabel during the repaired boot.
Permanently replace bad `audit=O` with required `audit=0` (digit zero)
in the GRUB defaults and every boot entry, and boot with the corrected argument.
Replace the bad fstab line with a valid persistent loop mount of
`/var/lib/labctl-media/lx401-recovery.img` at `/srv/recovery`, validate fstab,
and finish in a healthy multi-user boot. Do not disable SELinux.
EOF
chown student:student /home/student/LAB.md
# Stage relabel evidence last so setup cannot repair it before the learner reboots.
chcon -t user_tmp_t /etc/shadow
touch /.autorelabel
