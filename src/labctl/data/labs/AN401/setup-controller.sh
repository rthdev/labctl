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

if [ ! -e /home/student/LAB.md ]; then
    cat > /home/student/LAB.md <<'LAB_INSTRUCTIONS'
# AN401 — Repair fragile Ansible application automation

## Assignment

On controller, repair the supplied project in /home/student/ansible-lab. Do not
replace the exercise with manual target changes: grading resets target, refreshes
inventory, and runs site.yml twice. The target must serve the AN401 application,
keep its application configuration at root:apache mode 0640, and leave the second
playbook run with changed=0 and failures=0. Any Ansible structure or modules that
produce those outcomes are accepted.

## Required application state

- Enable and start httpd.
- /etc/an401/app.conf must contain exactly `environment=production` followed
  by one newline, owned by root:apache with mode 0640.
- http://127.0.0.1/ must return `AN401 application ready`.
- The second playbook recap must report changed=0, unreachable=0, and failed=0.

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
Inventory group(s): app.

When ready, run this command on the host where labctl is installed, not inside
this controller VM:

```sh
labctl grade AN401
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
