#!/bin/bash
set -euo pipefail
install -d -m 0700 /var/lib/labctl
# Setup is repeat-safe; never restage faults over an existing learner attempt.
test ! -e /var/lib/labctl/lx312-staged || exit 0
cat /proc/sys/kernel/random/boot_id > /var/lib/labctl/lx312-boot
dnf -y install python3 curl procps-ng strace
id lx312 >/dev/null 2>&1 || useradd --system --no-create-home --shell /sbin/nologin lx312
install -d -m 0755 /opt/lx312 /srv/lx312
truncate -s 48M /srv/lx312/dataset
chmod 0000 /srv/lx312/dataset
cat > /opt/lx312/app.py <<'PYAPP'
#!/usr/bin/python3
"""Fixed diagnostic workload: hold the entire supplied dataset in memory."""
import hashlib
import http.server
import os

with open(os.environ["DATA_FILE"], "rb") as stream:
    dataset = stream.read()
body = f"bytes={len(dataset)} sha256={hashlib.sha256(dataset).hexdigest()}\n".encode()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


http.server.HTTPServer(("127.0.0.1", 8312), Handler).serve_forever()
PYAPP
chmod 0644 /opt/lx312/app.py
sha256sum /opt/lx312/app.py /srv/lx312/dataset > /var/lib/labctl/lx312-original.sha256
cat > /etc/systemd/system/lx312-app.service <<'UNIT'
[Unit]
Description=Memory-resident diagnostic dataset service
After=network.target
[Service]
User=lx312
Environment=DATA_FILE=/srv/lx312/missing
ExecStart=/usr/bin/python3 /opt/lx312/app.py
MemoryMax=16M
MemorySwapMax=0
NoNewPrivileges=yes
Restart=no
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
# Generate initial failure evidence without blocking cloud-init readiness.
systemctl start lx312-app.service || true
systemctl disable --now lx312-app.service

touch /var/lib/labctl/lx312-staged
