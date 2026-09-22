#!/bin/sh
set -eu

dnf -y install httpd firewalld policycoreutils-python-utils python3
install -d -m 0755 /opt/an402 /var/www/html
printf 'ok\n' > /opt/an402/health
cat > /etc/systemd/system/an402-backend.service <<'EOF'
[Unit]
Description=AN402 health backend
After=network.target
[Service]
Type=simple
WorkingDirectory=/opt/an402
ExecStart=/usr/bin/python3 -m http.server 8080 --bind 127.0.0.1
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/httpd/conf.d/an402.conf <<'EOF'
ProxyPass /api/ http://127.0.0.1:8080/
ProxyPassReverse /api/ http://127.0.0.1:8080/
EOF
printf 'AN402 platform ready\n' > /var/www/html/index.html
systemctl daemon-reload
systemctl enable --now firewalld an402-backend httpd
firewall-cmd --permanent --add-service=http
firewall-cmd --reload
setsebool -P httpd_can_network_connect on

case "$(hostname -s)" in
  web1)
    ;;
  web2)
    printf 'maintenance\n' > /var/www/html/index.html
    systemctl disable --now httpd
    ;;
  web3)
    firewall-cmd --permanent --remove-service=http
    firewall-cmd --reload
    setsebool -P httpd_can_network_connect off
    systemctl disable --now an402-backend
    ;;
  *)
    echo "unexpected AN402 hostname" >&2
    exit 1
    ;;
esac
