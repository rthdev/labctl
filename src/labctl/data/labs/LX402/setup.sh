#!/bin/sh
set -eu
dnf -y install curl
systemctl disable --now lx402-api lx402-burner >/dev/null 2>&1 || true
install -d -m 0755 /var/lib/labctl-media /srv/incident
umount /srv/incident >/dev/null 2>&1 || true
truncate -s 384M /var/lib/labctl-media/lx402-incident.img
mkfs.ext4 -F /var/lib/labctl-media/lx402-incident.img >/dev/null
mount -o loop /var/lib/labctl-media/lx402-incident.img /srv/incident
fallocate -l 340M /srv/incident/runaway.log
printf '%s\n' 'LX402 healthy' > /srv/incident/health
cat > /usr/local/sbin/lx402-api.py <<'EOF'
#!/usr/bin/python3
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
stats = os.statvfs('/srv/incident')
used_ratio = 1 - (stats.f_bavail / stats.f_blocks)
if used_ratio >= 0.85:
 raise SystemExit('refusing startup under disk pressure')
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path != '/health': self.send_error(404); return
  body=open('/srv/incident/health','rb').read()
  self.send_response(200); self.end_headers(); self.wfile.write(body)
 def log_message(self, format, *args): pass
HTTPServer(('127.0.0.1',8090),Handler).serve_forever()
EOF
chmod 0755 /usr/local/sbin/lx402-api.py
cat > /etc/systemd/system/lx402-api.service <<'EOF'
[Unit]
Description=LX402 incident API
After=network.target
[Service]
ExecStart=/usr/local/sbin/lx402-api.py
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/lx402-burner.service <<'EOF'
[Unit]
Description=LX402 bounded runaway CPU symptom
[Service]
ExecStart=/usr/bin/sha256sum /dev/zero
Nice=19
CPUQuota=20%
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now lx402-burner
systemctl start lx402-api >/dev/null 2>&1 || true
rm -f /var/log/lx402-incident-report
cat > /home/student/LAB.md <<'EOF'
# LX402: Production incident diagnosis
Diagnose the failing API, pressure on the isolated 384 MiB incident filesystem,
and the intentionally CPU-capped burner (it cannot consume more than 20% CPU).
Remove the disposable `runaway.log`, disable/stop `lx402-burner`, and enable/start
`lx402-api` so `/health` returns `LX402 healthy`. Keep the lab-media mount intact.
Write `/var/log/lx402-incident-report` with exactly these two evidence lines:
`root cause: disk pressure and runaway burner`
`recovery: capacity restored; api enabled and healthy`
EOF
chown student:student /home/student/LAB.md
