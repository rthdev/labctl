#!/bin/sh
set -eu
if [ ! -x /usr/bin/ansible-playbook ]; then dnf -y install ansible-core; fi
install -d -m 0755 -o student -g student /home/student/ansible-lab
if [ ! -e /home/student/ansible-lab/site.yml ]; then
cat > /home/student/ansible-lab/site.yml <<'EOF'
---
- name: Perform an idempotent rolling web release
  hosts: web_fleet
  serial: 1
  become: true
  tasks: []
EOF
fi
chown -R student:student /home/student/ansible-lab
chmod 0644 /home/student/ansible-lab/site.yml

if [ ! -e /home/student/LAB.md ]; then
    cat > /home/student/LAB.md <<'LAB_INSTRUCTIONS'
# AN301 — Rolling Ansible web deployment

## Assignment

Complete /home/student/ansible-lab/site.yml on controller. Deploy nginx to all
hosts in the web_fleet group as a rolling update (use serial: 1). Each enabled,
active service must return the exact text "AN301 version 3.1.0". Make the
playbook fully idempotent: grading runs it twice and the second run must report
changed=0 for every host. Outcome grading does not claim to prove sequencing.

## Interactive practice from controller

You do not need to grade first to connect. From this controller, log in as
student to a managed VM using its SSH alias (exit to return to controller):

```sh
ssh web1
ssh web2
ssh web3
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
Inventory group(s): web_fleet.

When ready, run this command on the host where labctl is installed, not inside
this controller VM:

```sh
labctl grade AN301
```

Grading preserves controller and your project, resets only the managed VM(s)
(web1, web2, web3), supplies a fresh dynamic inventory and SSH access, then runs
/home/student/ansible-lab/site.yml from controller and checks the resulting state.
Do not rely on manual changes to managed VMs surviving grading. The temporary
grading inventory is removed afterwards; you do not need to create it yourself.
LAB_INSTRUCTIONS
    chown student:student /home/student/LAB.md
    chmod 0644 /home/student/LAB.md
fi
