#!/bin/bash
set -euo pipefail
install -d -m 0700 /var/lib/labctl
test ! -e /var/lib/labctl/lx310-staged || exit 0
dnf -y install e2fsprogs grubby grub2-tools
cat /proc/sys/kernel/random/boot_id > /var/lib/labctl/lx310-boot
getent shadow root | cut -d: -f2 > /var/lib/labctl/lx310-root
install -d -m 0755 /var/lib/labctl-media /srv/lx310
truncate -s 256M /var/lib/labctl-media/lx310.img
mkfs.ext4 -F /var/lib/labctl-media/lx310.img
mount -o loop /var/lib/labctl-media/lx310.img /srv/lx310
printf '%s\n' 'LX310 retained ledger' > /srv/lx310/ledger.txt
sha256sum /srv/lx310/ledger.txt > /var/lib/labctl/lx310-ledger.sha256
umount /srv/lx310
blkid -p -s UUID -o value /var/lib/labctl-media/lx310.img > /var/lib/labctl/lx310-fs-uuid
# Record the boot-entry set; recovery must not delete affected entries.
find /boot/loader/entries -maxdepth 1 -name '*.conf' -printf '%f\n' | sort > /var/lib/labctl/lx310-bls
# These faults apply at the learner's explicit next reboot, not during create.
printf '%s\n' 'UUID=00000000-0000-0000-0000-000000000310 /srv/lx310 ext4 defaults,x-systemd.device-timeout=10s 0 2' >> /etc/fstab
grubby --update-kernel=ALL --args=systemd.unit=rescue.target
python3 - <<'PY'
from pathlib import Path
p = Path('/etc/default/grub')
s = p.read_text()
# Cloud images may keep kernel arguments only in BLS entries, not defaults.
s += '\nGRUB_CMDLINE_LINUX="${GRUB_CMDLINE_LINUX:-} systemd.unit=rescue.target"\n'
p.write_text(s)
PY
# Kernel console= alone does not expose GRUB through labctl's serial console.
# Read the existing defaults without replacing any platform kernel arguments.
(
    source /etc/default/grub
    speed=115200
    if [[ ${GRUB_CMDLINE_LINUX:-} =~ console=ttyS0,([0-9]+) ]]; then
        speed=${BASH_REMATCH[1]}
    fi
    cat >> /etc/default/grub <<EOF

# LX310: make early boot recovery reachable on ttyS0 as well as VGA.
unset GRUB_TERMINAL
GRUB_TERMINAL_INPUT="console serial"
GRUB_TERMINAL_OUTPUT="console serial"
GRUB_SERIAL_COMMAND="serial --unit=0 --speed=${speed} --word=8 --parity=no --stop=1"
GRUB_TIMEOUT_STYLE=menu
GRUB_TIMEOUT=10
EOF
)
# Rocky 10 uses this canonical config on BIOS and UEFI; leave the EFI stub alone.
# Do not use --update-bls-cmdline: existing per-entry platform arguments survive.
grub2-editenv - unset menu_auto_hide
grub2-mkconfig -o /boot/grub2/grub.cfg
passwd -l root
systemctl set-default multi-user.target
chmod 0600 /var/lib/labctl/lx310-*
touch /var/lib/labctl/lx310-staged
