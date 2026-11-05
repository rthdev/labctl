#!/bin/bash
set -euo pipefail
install -d -m 0700 /var/lib/labctl
test ! -e /var/lib/labctl/lx311-staged || exit 0
dnf -y install e2fsprogs curl firewalld tcpdump iproute python3
cat /proc/sys/kernel/random/boot_id > /var/lib/labctl/lx311-boot
id lx311 >/dev/null 2>&1 || useradd --system --no-create-home --shell /sbin/nologin lx311
install -d -m 0755 /var/lib/labctl-media /srv/lx311 /opt/lx311
truncate -s 256M /var/lib/labctl-media/lx311.img
mkfs.ext4 -F /var/lib/labctl-media/lx311.img
mount -o loop /var/lib/labctl-media/lx311.img /srv/lx311
chown root:lx311 /srv/lx311
chmod 0770 /srv/lx311
printf '%s\n' 'LX311 retained ledger' > /srv/lx311/ledger.txt
chmod 0644 /srv/lx311/ledger.txt
sha256sum /srv/lx311/ledger.txt > /var/lib/labctl/lx311-ledger.sha256
blkid -p -s UUID -o value /var/lib/labctl-media/lx311.img > /var/lib/labctl/lx311-fs-uuid
cat > /opt/lx311/export.py <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import os

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/health':
            self.send_error(404)
            return
        body = Path('/srv/lx311/ledger.txt').read_bytes()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != '/probe':
            self.send_error(404)
            return
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 128:
            self.send_error(400)
            return
        self.connection.settimeout(5)
        body = self.rfile.read(length)
        try:
            Path('/srv/lx311/last-probe').write_bytes(body)
        except OSError:
            self.send_error(503, 'Storage is not writable')
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

HTTPServer((os.environ['BIND'], int(os.environ['PORT'])), Handler).serve_forever()
PY
printf '%s\n' 'BIND=127.0.0.1' 'PORT=8311' > /etc/lx311-export.conf
cat > /etc/systemd/system/lx311-export.service <<'UNIT'
[Unit]
Description=LX311 storage exporter
After=network.target
RequiresMountsFor=/srv/lx311
[Service]
User=lx311
Group=lx311
EnvironmentFile=/etc/lx311-export.conf
ExecStart=/usr/bin/python3 /opt/lx311/export.py
NoNewPrivileges=yes
[Install]
WantedBy=multi-user.target
UNIT
sha256sum /opt/lx311/export.py > /var/lib/labctl/lx311-program.sha256
printf '%s\n' '/var/lib/labctl-media/lx311.img /srv/lx311 ext4 loop,ro 0 2' >> /etc/fstab
mount -o remount,ro /srv/lx311
systemctl daemon-reload
systemctl enable --now lx311-export.service firewalld
interface=$(ip -o route show default | awk 'NR==1 {print $5}')
connection=$(nmcli -g GENERAL.CONNECTION device show "$interface")
nmcli connection modify "$connection" connection.zone public
firewall-cmd --zone=public --change-interface="$interface"
firewall-cmd --permanent --zone=public --add-service=ssh
firewall-cmd --permanent --zone=public --remove-port=8311/tcp || test "$?" -eq 0
firewall-cmd --reload
chmod 0600 /var/lib/labctl/lx311-*
touch /var/lib/labctl/lx311-staged
