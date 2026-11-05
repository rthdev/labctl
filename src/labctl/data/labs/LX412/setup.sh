#!/bin/bash
set -euo pipefail
install -d -m 0700 /var/lib/lx412
if test -f /var/lib/lx412/staged; then exit 0; fi
dnf -y install httpd python3 curl policycoreutils-python-utils
install -d -m 0755 /srv/lx412/public /usr/local/sbin /etc/systemd/journald.conf.d
printf 'release LX412 production\n' > /srv/lx412/public/release.txt
printf '125\n250\n75\n' > /srv/lx412/records.csv
sha256sum /srv/lx412/public/release.txt /srv/lx412/records.csv > /var/lib/lx412/data.sha256
id lx412-app &>/dev/null || useradd -M -s /sbin/nologin lx412-app
cat > /etc/lx412-worker.json <<'EOF'
{"history_limit": 128}
EOF
cat > /usr/local/sbin/lx412-worker.py <<'EOF'
#!/usr/bin/python3
# BEGIN WORKER
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path


class Ledger:
    def __init__(self, records, history_limit):
        self.records = Path(records)
        if type(history_limit) is not int or not 0 <= history_limit <= 128:
            raise ValueError('history_limit must be an integer between 0 and 128')
        self.history = deque(maxlen=history_limit)

    def response(self):
        total = sum(int(line) for line in self.records.read_text().splitlines())
        # A bounded model of accidentally retained diagnostic request buffers.
        self.history.append(bytearray(512 * 1024))
        print(f'request complete; retained_buffers={len(self.history)}', flush=True)
        return f'{total}\n'.encode()


def main():
    config = json.loads(Path('/etc/lx412-worker.json').read_text())
    ledger = Ledger('/srv/lx412/records.csv', config['history_limit'])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/total':
                self.send_error(404)
                return
            body = ledger.response()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Labctl-Probe', self.headers.get('X-Labctl-Probe', '')[:128])
            self.end_headers()
            self.wfile.write(body)

    HTTPServer(('127.0.0.1', 9412), Handler).serve_forever()


if __name__ == '__main__':
    main()
# END WORKER
EOF
chmod 0755 /usr/local/sbin/lx412-worker.py
cat > /etc/systemd/system/lx412-worker.service <<'EOF'
[Unit]
Description=LX412 ledger aggregation worker
After=network.target
[Service]
User=lx412-app
ExecStart=/usr/local/sbin/lx412-worker.py
MemoryAccounting=yes
MemoryMax=96M
CPUQuota=25%
NoNewPrivileges=yes
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/httpd/conf.d/lx412.conf <<'EOF'
Listen 127.0.0.1:8412
<VirtualHost 127.0.0.1:8412>
    ServerName lx412.lab
    DocumentRoot /srv/lx412/public
    <Directory /srv/lx412/public>
        Require all granted
    </Directory>
    ProxyPass /api/ http://127.0.0.1:9413/
    ProxyPassReverse /api/ http://127.0.0.1:9413/
    ErrorLog /proc/self/fd/2
    LogFormat "%{X-Labctl-Probe}i %f %P" lx412_evidence
    CustomLog /var/log/httpd/lx412-access.log lx412_evidence
</VirtualHost>
EOF
# Baseline network permission; only lab content labeling is a staged SELinux fault.
semanage port -a -t http_port_t -p tcp 8412
setsebool -P httpd_can_network_connect on
semanage fcontext -a -t var_t '/srv/lx412/public(/.*)?'
restorecon -RF /srv/lx412/public
cat > /etc/systemd/journald.conf.d/lx412.conf <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=64M
EOF
mkdir -p /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal
systemctl restart systemd-journald
journalctl --flush
sha256sum /usr/local/sbin/lx412-worker.py > /var/lib/lx412/program.sha256
cat /proc/sys/kernel/random/boot_id > /var/lib/lx412/boot-id
systemctl daemon-reload
systemctl enable --now lx412-worker httpd
# Bounded guest-only load seeds inspectable retained-memory evidence.
for attempt in {1..20}; do
    if curl --max-time 1 -fsS http://127.0.0.1:9412/total >/dev/null; then break; fi
    sleep 0.2
done
for request in {1..96}; do curl --max-time 2 -fsS http://127.0.0.1:9412/total >/dev/null; done
touch /var/lib/lx412/staged
