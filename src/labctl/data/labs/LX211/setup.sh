#!/bin/bash
set -euo pipefail
install -d -m 0700 /var/lib/labctl
# Setup is repeat-safe; never restage faults over an existing learner attempt.
test ! -e /var/lib/labctl/lx211-staged || exit 0
cat /proc/sys/kernel/random/boot_id > /var/lib/labctl/lx211-boot
dnf -y install httpd firewalld curl policycoreutils-python-utils
install -d -m 0755 /srv/lx211/site
printf '%s\n' 'LX211 secured records' > /srv/lx211/site/health
chcon -R -t user_tmp_t /srv/lx211/site
cat > /etc/httpd/conf.d/lx211.conf <<'HTTP'
Listen 8088
<VirtualHost *:8088>
DocumentRoot /srv/lx211/site
<Directory /srv/lx211/site>
Require all granted
AllowOverride None
</Directory>
</VirtualHost>
HTTP
systemctl enable --now firewalld
device=$(ip -o route show default | awk 'NR==1 {print $5}')
connection=$(nmcli -g GENERAL.CONNECTION device show "$device")
nmcli connection modify "$connection" connection.zone public
firewall-cmd --zone=public --change-interface="$device"
firewall-cmd --permanent --zone=public --add-service=cockpit
firewall-cmd --reload
systemctl disable --now httpd

touch /var/lib/labctl/lx211-staged
