#!/bin/sh
set -eu
dnf -y install httpd policycoreutils-python-utils curl
install -d -m 0755 /srv/secureweb
printf '%s\n' 'LX301 secure service' > /srv/secureweb/index.html
chown -R root:root /srv/secureweb
chmod 0755 /srv/secureweb; chmod 0644 /srv/secureweb/index.html
rm -f /etc/httpd/conf.d/lx301.conf
semanage port -d -t http_port_t -p tcp 8088 >/dev/null 2>&1 || true
semanage fcontext -d '/srv/secureweb(/.*)?' >/dev/null 2>&1 || true
chcon -t default_t /srv/secureweb /srv/secureweb/index.html || true
systemctl disable --now httpd >/dev/null 2>&1 || true
setenforce 1
sed -i 's/^SELINUX=.*/SELINUX=enforcing/' /etc/selinux/config
semanage permissive -d httpd_t >/dev/null 2>&1 || true
cat > /home/student/LAB.md <<'EOF'
# LX301: Secure web service with SELinux
Configure httpd to serve `/srv/secureweb` on TCP 8088. Add persistent SELinux
port and file-context policy, apply it with restorecon, then enable/start httpd.
Keep SELinux enforcing; disabling it or using a broad permissive workaround fails.
EOF
chown student:student /home/student/LAB.md
