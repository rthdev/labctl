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

if [ ! -e /home/student/LAB.md ]; then
    cat > /home/student/LAB.md <<'LAB_INSTRUCTIONS'
# AN002 — Manage users and shared access with Ansible

## Assignment

On controller, work in /home/student/ansible-lab and complete site.yml.
Target the managed host group from the inventory supplied during grading. Create
the automation group and the deploy and auditor users, with both users in that
group. Create /srv/automation as root:automation mode 2770. Install the public
key from /home/student/ansible-lab/deploy_access_key.pub for deploy in an
authorized_keys file as its sole key, owned by deploy:deploy with mode 0600. Grading resets only
target, runs your playbook from the persistent controller, and checks outcomes.

## Interactive practice from controller

You do not need to grade first to connect. From this controller, log in as
student to a managed VM using its SSH alias (exit to return to controller):

```sh
ssh target
```

To test your automation from controller:

```sh
cd /home/student/ansible-lab
ansible all -i inventory.ini -m ping
ansible-playbook -i inventory.ini site.yml
```

labctl manages inventory.ini and the practice SSH connection settings, using a
dedicated controller key and verified target host keys. Do not disable host-key
checking or copy a host-management private key into this VM. Keep custom
inventories in a separate file; labctl refreshes the managed connection files
after resets. Your playbook and supporting project files are not replaced by
connection refresh. A full lab reset still discards controller work.

Practice runs change the current targets. Grading instead resets managed targets
and uses a separate temporary inventory, so your playbook must work from the
lab's clean starting state without manual target preparation.

## Workflow and grading

Work on controller in /home/student/ansible-lab; the entry point is site.yml.
Inventory group(s): managed.

When ready, run this command on the host where labctl is installed, not inside
this controller VM:

```sh
labctl grade AN002
```

Grading preserves controller and your project, resets only the managed VM(s)
(target), supplies a fresh dynamic inventory and SSH access, then runs
/home/student/ansible-lab/site.yml from controller and checks the resulting state.
Do not rely on manual changes to managed VMs surviving grading. The temporary
grading inventory is removed afterwards; you do not need to create it yourself.
LAB_INSTRUCTIONS
    chown student:student /home/student/LAB.md
    chmod 0644 /home/student/LAB.md
fi
