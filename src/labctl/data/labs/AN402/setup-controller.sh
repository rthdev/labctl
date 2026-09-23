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

if [ ! -e /home/student/LAB.md ]; then
    cat > /home/student/LAB.md <<'LAB_INSTRUCTIONS'
# AN402 — Recover a multi-node service platform with Ansible

## Assignment

On controller, repair the supplied project in /home/student/ansible-lab. Grading
resets all managed nodes to deterministic starting faults, refreshes inventory,
and runs site.yml twice. Recover web service, application configuration, firewall,
enforcing SELinux policy, and backend health on every node. Preserve already
healthy state through idempotent automation. Generate
/home/student/ansible-lab/recovery-summary.json on controller with lab, status,
and one healthy entry for each managed host. Outcomes are graded; playbook and
role structure are not.

## Required platform state

Managed nodes: web1, web2, web3 (inventory group: platform).
Initially web1 is healthy; web2 has a stopped/disabled web service and wrong
landing content; web3 has blocked HTTP, an incorrect SELinux boolean, and a
stopped/disabled backend. Preserve healthy state while repairing the faults.

On every managed node:
- Enable and start httpd and an402-backend.
- Serve `AN402 platform ready` from http://127.0.0.1/.
- Enable firewalld and allow its http service at runtime and permanently.
- Keep SELinux enforcing and enable httpd_can_network_connect.
- http://127.0.0.1/api/health must return `ok`.
- /etc/httpd/conf.d/an402.conf must contain exactly these two lines, each
  terminated by a newline:

```apache
ProxyPass /api/ http://127.0.0.1:8080/
ProxyPassReverse /api/ http://127.0.0.1:8080/
```

Generate /home/student/ansible-lab/recovery-summary.json on controller:

```json
{
  "lab": "AN402",
  "status": "recovered",
  "hosts": [
    {"name": "web1", "status": "healthy"},
    {"name": "web2", "status": "healthy"},
    {"name": "web3", "status": "healthy"}
  ]
}
```

Host order does not matter; exactly one healthy entry per managed host is required.
Grading removes any old summary before running your playbook twice. The second
recap must report changed=0, unreachable=0, and failed=0 for every managed host.

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
Inventory group(s): platform.

When ready, run this command on the host where labctl is installed, not inside
this controller VM:

```sh
labctl grade AN402
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
