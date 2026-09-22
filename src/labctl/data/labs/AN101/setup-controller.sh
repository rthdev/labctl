#!/bin/sh
set -eu

if [ ! -x /usr/bin/ansible-playbook ]; then
    dnf -y install ansible-core
fi
install -d -m 0755 -o student -g student /home/student/ansible-lab/templates
if [ ! -e /home/student/ansible-lab/site.yml ]; then
    cat > /home/student/ansible-lab/site.yml <<'EOF'
---
- name: Configure time synchronization
  hosts: managed
  become: true
  gather_facts: false
  tasks:
    - name: Replace this task with the AN101 template and service configuration
      ansible.builtin.debug:
        msg: Render chrony.conf and notify a restart handler only when it changes.
EOF
fi
chown -R student:student /home/student/ansible-lab
chmod 0644 /home/student/ansible-lab/site.yml
