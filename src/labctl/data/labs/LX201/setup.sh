#!/bin/sh
set -eu
dnf -y install httpd firewalld curl
install -d -m 0700 /var/lib/labctl
default_device=$(ip -o route show default | awk 'NR == 1 {print $5}')
connection=$(nmcli -g GENERAL.CONNECTION device show "$default_device")
uuid=$(nmcli -g connection.uuid connection show "$connection")
address=$(nmcli -g IP4.ADDRESS device show "$default_device" | head -n 1)
printf '%s\n%s\n%s\n' "$default_device" "$uuid" "$address" > /var/lib/labctl/lx201-network-baseline
chmod 0600 /var/lib/labctl/lx201-network-baseline
printf '%s\n' 'LX201 reachable' > /var/www/html/network-health
systemctl enable --now httpd NetworkManager
systemctl enable --now firewalld
firewall-cmd --permanent --zone=public --remove-service=http >/dev/null 2>&1 || true
firewall-cmd --permanent --zone=public --add-service=cockpit >/dev/null
firewall-cmd --reload >/dev/null
systemctl disable --now firewalld
