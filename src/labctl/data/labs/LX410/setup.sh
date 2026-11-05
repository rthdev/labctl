#!/bin/bash
set -euo pipefail
# Setup is guest-only; a completed setup is never replayed over learner repairs.
install -d -m 0700 /var/lib/lx410
if test -f /var/lib/lx410/staged; then exit 0; fi
dnf -y install gcc pam-devel python3 curl
install -d -m 0755 /usr/local/libexec /srv/lx410
for group in lx410-operators lx410-readers; do getent group "$group" || groupadd "$group"; done
for user in lx410-agent lx410-expired lx410-outsider lx410-app; do
    id "$user" &>/dev/null || useradd -M -s /sbin/nologin "$user"
done
usermod -aG lx410-operators lx410-agent
usermod -aG lx410-operators lx410-expired
chage -E 1 lx410-agent
chage -E 1 lx410-expired
cat > /etc/pam.d/lx410 <<'EOF'
# Dedicated account-admission stack; never used by SSH.
account required pam_unix.so
account required pam_access.so accessfile=/etc/security/lx410-access.conf
EOF
cat > /etc/security/lx410-access.conf <<'EOF'
+ : (lx410-retired) : ALL
- : ALL : ALL
EOF
cat > /usr/local/libexec/lx410-account.c <<'EOF'
#include <security/pam_appl.h>
#include <security/pam_misc.h>
#include <stdio.h>
int main(int argc, char **argv) {
    pam_handle_t *pamh = NULL;
    struct pam_conv conv = {misc_conv, NULL};
    if (argc != 2) return 2;
    int rc = pam_start("lx410", argv[1], &conv, &pamh);
    if (rc == PAM_SUCCESS) rc = pam_acct_mgmt(pamh, PAM_SILENT);
    if (pamh) pam_end(pamh, rc);
    puts(rc == PAM_SUCCESS ? "admitted" : "denied");
    return rc == PAM_SUCCESS ? 0 : 1;
}
EOF
gcc -Wall -Werror -o /usr/local/libexec/lx410-account /usr/local/libexec/lx410-account.c -lpam -lpam_misc
printf 'Dispatch batch 410: preserve customer queue\n' > /srv/lx410/dispatch.txt
chown root:lx410-readers /srv/lx410/dispatch.txt
chmod 0640 /srv/lx410/dispatch.txt
sha256sum /srv/lx410/dispatch.txt > /var/lib/lx410/data.sha256
cat > /etc/systemd/system/lx410-admission.service <<'EOF'
[Unit]
Description=LX410 dispatch account admission
Before=lx410-api.service
[Service]
Type=oneshot
ExecStart=/usr/local/libexec/lx410-account lx410-agent
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/lx410-api.service <<'EOF'
[Unit]
Description=LX410 dispatch service
Requires=lx410-admission.service
After=lx410-admission.service
[Service]
User=lx410-app
ExecStart=/usr/bin/python3 -m http.server 8410 --bind 127.0.0.1 --directory /srv/lx410
NoNewPrivileges=yes
[Install]
WantedBy=multi-user.target
EOF
sha256sum /usr/local/libexec/lx410-account /usr/local/libexec/lx410-account.c > /var/lib/lx410/program.sha256
cat /proc/sys/kernel/random/boot_id > /var/lib/lx410/boot-id
systemctl daemon-reload
systemctl enable lx410-api lx410-admission
systemctl start lx410-api || true
touch /var/lib/lx410/staged
