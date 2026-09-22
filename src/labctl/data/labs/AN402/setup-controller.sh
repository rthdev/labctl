#!/bin/sh
set -eu

if [ ! -x /usr/bin/ansible-playbook ]; then
    dnf -y install ansible-core
fi
install -d -m 0755 -o student -g student \
    /home/student/ansible-lab/roles/platform/tasks \
    /home/student/ansible-lab/roles/platform/templates
cat > /home/student/ansible-lab/site.yml <<'EOF'
---
- name: Recover the AN402 platform
  hosts: platform
  become: true
  gather_facts: true
  roles:
    - platform
# Add a controller-side task that writes recovery-summary.json after recovery.
EOF
cat > /home/student/ansible-lab/roles/platform/tasks/main.yml <<'EOF'
---
# Each managed node begins with a different fault. Replace this diagnostic-only
# starter with idempotent recovery of every required platform outcome.
- name: Inspect the web service without recovering it
  ansible.builtin.command: systemctl is-active httpd
  changed_when: true
  failed_when: false
EOF
cat > /home/student/ansible-lab/roles/platform/templates/README.j2 <<'EOF'
The recovered landing page must contain exactly: AN402 platform ready
EOF
chown -R student:student /home/student/ansible-lab
find /home/student/ansible-lab -type d -exec chmod 0755 {} +
find /home/student/ansible-lab -type f -exec chmod 0644 {} +
