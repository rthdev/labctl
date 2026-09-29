#!/bin/sh
set -eu

if [ ! -x /usr/bin/ansible-playbook ]; then
    dnf -y install ansible-core
fi
install -d -m 0755 -o student -g student /home/student/ansible-lab
if [ ! -e /home/student/ansible-lab/site.yml ]; then
    cat > /home/student/ansible-lab/site.yml <<'EOF'
---
- name: Configure the web target
  hosts: web
  become: true
  gather_facts: false
  tasks:
    - name: Replace this task with an idempotent nginx configuration
      ansible.builtin.debug:
        msg: Complete the package, service, and content tasks for AN001.
EOF
fi
chown -R student:student /home/student/ansible-lab
chmod 0644 /home/student/ansible-lab/site.yml
