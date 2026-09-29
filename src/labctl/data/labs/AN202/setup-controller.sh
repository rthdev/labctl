#!/bin/sh
set -eu
if [ ! -x /usr/bin/ansible-playbook ]; then dnf -y install ansible-core; fi
install -d -m 0755 -o student -g student /home/student/ansible-lab
if [ ! -e /home/student/ansible-lab/site.yml ]; then
cat > /home/student/ansible-lab/site.yml <<'EOF'
---
- name: Configure durable secure web storage
  hosts: storage_web
  become: true
  tasks: []
EOF
fi
chown -R student:student /home/student/ansible-lab
chmod 0644 /home/student/ansible-lab/site.yml

if [ ! -e /home/student/LAB.md ]; then
    cat > /home/student/LAB.md <<'LAB_INSTRUCTIONS'
# AN202 — Ansible storage, web, firewall, and SELinux

## Assignment

Complete /home/student/ansible-lab/site.yml on controller. On target, create a
1 GiB sparse /var/lib/an202/storage.img ext4 filesystem, persistently mount it
at /srv/an202 using the loop option. Configure enabled, active httpd to serve
/srv/an202/www as its live web root, with /srv/an202/www/index.html containing
the exact text "AN202 durable web"; do not copy it to an independent default
document root. Permanently allow HTTP through firewalld. Keep SELinux enforcing
and assign correct persistent httpd content contexts; do not disable SELinux.

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

Practice runs change the current targets. Default grading uses that current state
without resetting disks; it still executes your playbook using a separate temporary
inventory. Use --reset to prove it works from the lab's clean starting state.

## Workflow and grading

Work on controller in /home/student/ansible-lab; the entry point is site.yml.
Inventory group(s): storage_web.

When ready, run this command on the host where labctl is installed, not inside
this controller VM:

```sh
labctl grade AN202
# Optional destructive clean-baseline check:
labctl grade AN202 --reset
```

Default current-state grading does not reset or start VMs. Start stopped labs
on the host with `labctl lab start AN202` first. Both modes execute your playbook.
The clean-baseline --reset mode names the target disks to destroy and asks [y/N]
(default No). Add --yes only to bypass confirmation; noninteractive, JSON, and
quiet reset runs require --yes. The controller and learner project are preserved.

With --reset, grading preserves controller and your project, resets only the managed VM(s)
(target), supplies a fresh dynamic inventory and SSH access, then runs
/home/student/ansible-lab/site.yml from controller and checks the resulting state.
Do not rely on manual changes surviving --reset or the playbook itself. The temporary
grading inventory is removed afterwards; you do not need to create it yourself.
LAB_INSTRUCTIONS
    chown student:student /home/student/LAB.md
    chmod 0644 /home/student/LAB.md
fi
