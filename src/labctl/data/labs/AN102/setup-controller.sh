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

if [ ! -e /home/student/LAB.md ]; then
    cat > /home/student/LAB.md <<'LAB_INSTRUCTIONS'
# AN102 — Build an idempotent reusable web role

## Assignment

On controller, work in /home/student/ansible-lab. Implement the web automation,
preferably as the provided reusable role, and apply it from site.yml to the web
host group supplied during grading. Install nginx, enable and start it, and
deploy /usr/share/nginx/html/index.html as root:root mode 0644 with the exact
content "Reusable Ansible role" followed by one newline. The complete playbook
must report changed=0 on a second run. Grading accepts any playbook structure:
it checks only the web outcomes and the second ansible-playbook recap after
resetting target and running from the persistent controller.

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
Inventory group(s): web.

When ready, run this command on the host where labctl is installed, not inside
this controller VM:

```sh
labctl grade AN102
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
