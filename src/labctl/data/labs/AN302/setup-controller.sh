#!/bin/sh
set -eu
if [ ! -x /usr/bin/ansible-playbook ]; then dnf -y install ansible-core; fi
install -d -m 0755 -o student -g student /home/student/ansible-lab
if [ ! -e /home/student/ansible-lab/site.yml ]; then
cat > /home/student/ansible-lab/site.yml <<'EOF'
---
- name: Configure least-privilege accounts and secret
  hosts: identity_targets
  become: true
  tasks: []
EOF
fi
chown -R student:student /home/student/ansible-lab
chmod 0644 /home/student/ansible-lab/site.yml

if [ ! -e /home/student/LAB.md ]; then
    cat > /home/student/LAB.md <<'LAB_INSTRUCTIONS'
# AN302 — Ansible accounts, secrets, and narrow privilege

## Assignment

Complete /home/student/ansible-lab/site.yml on controller. Create dedicated
deployer and auditor accounts with private primary groups and no cross-membership.
Create /etc/an302/secret containing exactly "AN302-rotation-key" (no newline),
owned root:auditor mode 0640. Create /etc/sudoers.d/an302-deployer owned root:root
mode 0440 with exactly this single line:
deployer ALL=(root) /usr/bin/systemctl restart an302-agent
The deployer account must not be able to read the secret.

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
Inventory group(s): identity_targets.

When ready, run this command on the host where labctl is installed, not inside
this controller VM:

```sh
labctl grade AN302
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
