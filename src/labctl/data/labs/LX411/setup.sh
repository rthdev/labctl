#!/bin/bash
set -euo pipefail
install -d -m 0700 /var/lib/lx411
if test -f /var/lib/lx411/staged; then exit 0; fi
dnf -y install cryptsetup lvm2 e2fsprogs python3 curl
# Never format an existing incomplete setup: preserve it for diagnosis.
if test -e /var/lib/lx411/ledger.img; then
    echo 'Existing unstaged ledger image; refusing destructive replay' >&2
    exit 1
fi
install -d -m 0755 /srv/lx411 /usr/local/sbin
umask 077
head -c 64 /dev/urandom > /root/lx411-recovery.key
truncate -s 768M /var/lib/lx411/ledger.img
loop=$(losetup --find --show /var/lib/lx411/ledger.img)
cryptsetup luksFormat --type luks2 --batch-mode --pbkdf pbkdf2 --key-file /root/lx411-recovery.key "$loop"
cryptsetup luksUUID "$loop" > /var/lib/lx411/luks-uuid
cryptsetup open --key-file /root/lx411-recovery.key "$loop" lx411-crypt
pvcreate --devices /dev/mapper/lx411-crypt /dev/mapper/lx411-crypt
vgcreate --devices /dev/mapper/lx411-crypt lx411-vg /dev/mapper/lx411-crypt
lvcreate --devices /dev/mapper/lx411-crypt -L 512M -n ledger lx411-vg
mkfs.ext4 /dev/lx411-vg/ledger
{ pvs --devices /dev/mapper/lx411-crypt --noheadings -o pv_uuid,vg_uuid /dev/mapper/lx411-crypt | xargs; lvs --devices /dev/mapper/lx411-crypt --noheadings -o lv_uuid /dev/lx411-vg/ledger | xargs; blkid -s UUID -o value /dev/lx411-vg/ledger; } > /var/lib/lx411/stack-ids
mount /dev/lx411-vg/ledger /srv/lx411
printf 'account,balance\nreserve,10411\npayroll,2411\n' > /srv/lx411/ledger.csv
chmod 0644 /srv/lx411/ledger.csv
sha256sum /srv/lx411/ledger.csv > /var/lib/lx411/data.sha256
umount /srv/lx411
lvchange --devices /dev/mapper/lx411-crypt -pr /dev/lx411-vg/ledger
vgchange --devices /dev/mapper/lx411-crypt -an lx411-vg
cryptsetup close lx411-crypt
losetup -d "$loop"
cat > /etc/lx411-storage.conf <<'EOF'
KEY_FILE=/root/lx411-retired.key
EOF
cat > /usr/local/sbin/lx411-attach <<'EOF'
#!/bin/bash
set -euo pipefail
source /etc/lx411-storage.conf
loop=$(losetup -j /var/lib/lx411/ledger.img --output NAME --noheadings)
if test -z "$loop"; then loop=$(losetup --find --show /var/lib/lx411/ledger.img); fi
if ! cryptsetup status lx411-crypt >/dev/null; then
    cryptsetup open --key-file "$KEY_FILE" "$loop" lx411-crypt
fi
vgchange -ay --devices /dev/mapper/lx411-crypt lx411-vg
EOF
chmod 0755 /usr/local/sbin/lx411-attach
cat > /etc/systemd/system/lx411-stack.service <<'EOF'
[Unit]
Description=LX411 isolated encrypted ledger stack
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/lx411-attach
RemainAfterExit=yes
TimeoutStartSec=30
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/srv-lx411.mount <<'EOF'
[Unit]
Description=LX411 ledger mount
Requires=lx411-stack.service
After=lx411-stack.service
DefaultDependencies=no
Before=lx411-api.service
Conflicts=umount.target
Before=umount.target
[Mount]
What=/dev/lx411-vg/ledger
Where=/srv/lx411
Type=ext4
Options=rw
TimeoutSec=30
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/lx411-api.conf <<'EOF'
DATA_ROOT=/srv/lx411-retired
EOF
cat > /usr/local/sbin/lx411-api.py <<'EOF'
#!/usr/bin/python3
import os
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
root = Path(os.environ['DATA_ROOT'])
# The bounded write makes a read-only recovery insufficient.
(root / 'service-write-test').write_text('ledger writer active\n')
os.chdir(root)
class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        # Fresh bounded receipt proves this process can still write this mount.
        token = self.headers.get('X-Labctl-Probe', '')
        if token and len(token) <= 128:
            (root / 'service-write-test').write_text(token + '\n')
        super().do_GET()

HTTPServer(('127.0.0.1', 8411), Handler).serve_forever()
EOF
chmod 0755 /usr/local/sbin/lx411-api.py
cat > /etc/systemd/system/lx411-api.service <<'EOF'
[Unit]
Description=LX411 ledger API
Requires=srv-lx411.mount
After=srv-lx411.mount
[Service]
EnvironmentFile=/etc/lx411-api.conf
ExecStart=/usr/local/sbin/lx411-api.py
NoNewPrivileges=yes
[Install]
WantedBy=multi-user.target
EOF
sha256sum /usr/local/sbin/lx411-api.py > /var/lib/lx411/program.sha256
cat /proc/sys/kernel/random/boot_id > /var/lib/lx411/boot-id
systemctl daemon-reload
systemctl enable lx411-stack srv-lx411.mount lx411-api
systemctl start lx411-api || true
touch /var/lib/lx411/staged
