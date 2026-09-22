#!/bin/sh
set -eu

if [ ! -x /usr/bin/ansible-playbook ]; then
    dnf -y install ansible-core
fi
root=/home/student/ansible-lab
install -d -m 0755 -o student -g student "$root/roles/web/tasks" "$root/roles/web/templates"
if [ ! -e "$root/site.yml" ]; then
    cat > "$root/site.yml" <<'EOF'
---
- name: Apply reusable web automation
  hosts: web
  become: true
  gather_facts: false
  roles:
    - web
EOF
fi
if [ ! -e "$root/roles/web/tasks/main.yml" ]; then
    cat > "$root/roles/web/tasks/main.yml" <<'EOF'
---
- name: Replace this task with idempotent AN102 web automation
  ansible.builtin.debug:
    msg: Install and configure nginx, then deploy the required exact content.
EOF
fi
chown -R student:student "$root"
find "$root" -type d -exec chmod 0755 {} +
find "$root" -type f -exec chmod 0644 {} +
