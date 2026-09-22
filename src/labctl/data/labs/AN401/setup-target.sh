#!/bin/sh
set -eu

dnf -y install httpd
install -d -m 0755 /etc/an401 /var/www/html
printf 'environment=development\n' > /etc/an401/app.conf
chown student:student /etc/an401/app.conf
chmod 0666 /etc/an401/app.conf
printf 'fragile starter state\n' > /var/www/html/index.html
systemctl disable --now httpd || true
