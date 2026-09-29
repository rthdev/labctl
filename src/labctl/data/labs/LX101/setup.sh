#!/bin/sh
set -eu
dnf -y install curl
dnf -y remove httpd >/dev/null 2>&1 || true
rm -f /var/www/html/health /etc/systemd/journald.conf.d/lx101.conf
