#!/bin/sh
set -eu

if [ ! -x /usr/bin/ansible-playbook ]; then
    dnf -y install ansible-core
fi
install -d -m 0755 -o student -g student \
    /home/student/ansible-lab/roles/web/tasks \
    /home/student/ansible-lab/roles/web/files
cat > /home/student/ansible-lab/site.yml <<'EOF'
---
- name: Repair and configure the AN401 application
  hosts: app
  become: true
  gather_facts: false
  roles:
    - web
EOF
cat > /home/student/ansible-lab/roles/web/tasks/main.yml <<'EOF'
---
# This role is deliberately fragile. Repair it rather than trusting its choices.
- name: Install the web package (non-idempotent command)
  ansible.builtin.command: dnf -y install httpd
- name: Deploy application configuration with unsafe permissions
  ansible.builtin.copy:
    content: "environment=development\n"
    dest: /etc/an401/app.conf
    mode: "0666"
- name: Deploy placeholder content
  ansible.builtin.copy:
    content: "unfinished\n"
    dest: /var/www/html/index.html
- name: Restart the service on every run
  ansible.builtin.command: systemctl restart httpd
EOF
cat > /home/student/ansible-lab/roles/web/files/README <<'EOF'
The finished page must contain exactly: AN401 application ready
EOF
chown -R student:student /home/student/ansible-lab
find /home/student/ansible-lab -type d -exec chmod 0755 {} +
find /home/student/ansible-lab -type f -exec chmod 0644 {} +
