#!/bin/sh
set -eu

if [ ! -x /usr/bin/ansible-playbook ]; then
    dnf -y install ansible-core
fi
install -d -m 0755 -o student -g student /home/student/ansible-lab
if [ ! -f /home/student/ansible-lab/deploy_access_key ]; then
    runuser -u student -- /usr/bin/ssh-keygen -q -t ed25519 -N '' \
        -f /home/student/ansible-lab/deploy_access_key
fi
if [ ! -e /home/student/ansible-lab/site.yml ]; then
    cat > /home/student/ansible-lab/site.yml <<'EOF'
---
- name: Manage team access
  hosts: managed
  become: true
  gather_facts: false
  tasks:
    - name: Replace this task with idempotent account and access management
      ansible.builtin.debug:
        msg: Complete the users, group, directory, and authorized key outcomes for AN002.
EOF
fi
chown -R student:student /home/student/ansible-lab
chmod 0600 /home/student/ansible-lab/deploy_access_key
chmod 0644 /home/student/ansible-lab/deploy_access_key.pub /home/student/ansible-lab/site.yml
