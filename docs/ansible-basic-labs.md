# Introductory Ansible labs

Every controller includes the lab assignment and grading workflow in
`/home/student/LAB.md`, owned by `student:student` with mode `0644`. Setup
creates the guide only if absent, preserving learner notes on subsequent runs.
Run `labctl grade LAB_ID` on the host, not inside the controller.

## Interactive practice

After creating the lab, enter the controller from the host:

```sh
labctl lab ssh LAB_ID controller
```

Inside the controller, use `ssh target` for a two-machine lab; multi-node labs
use their VM names (`ssh app`, `ssh proxy`, or `ssh web1`, `ssh web2`, `ssh web3`).
The controller's `LAB.md` lists the aliases for that exercise. No grading run
is needed to establish access. Return to controller with `exit`.

Run Ansible directly on controller:

```sh
cd /home/student/ansible-lab
ansible all -i inventory.ini -m ping
ansible-playbook -i inventory.ini site.yml
```

labctl manages the practice inventory and SSH connection settings, with a
separate controller key and pinned target host keys. Keep custom inventory
files under another name: managed connection files are refreshed after resets.
Refresh does not overwrite `site.yml`, roles, or other learner-authored project
files. A full lab reset still discards controller work; grading resets only the
managed targets. Graders retain their separate temporary inventories and assess
the playbook against the clean starting state, not manual target modifications.

## Existing labs

This guide is installed when creating a lab from the updated definitions.
Existing VMs and their saved setup snapshots are not updated automatically;
resetting an old lab does not pick up this change. Back up learner work before
recreating a lab. Do not rerun an entire controller setup script just to add the
guide: advanced-lab setup can overwrite the starter project.

AN001, AN002, AN101, and AN102 use the same two-machine workflow:

- `controller` persists between grading attempts. Learner work lives in
  `/home/student/ansible-lab/site.yml` (with supporting files beneath the same directory).
- `target` is reset immediately before grading so the playbook must converge a clean Rocky
  Linux 9 host.
- The grader creates a lab-specific controller SSH key, adds only its public key to the target,
  and writes a temporary inventory using the refreshed target address and pinned host key.
  The labctl management private key is never copied to the controller.
- Graders execute the learner playbook and inspect resulting target state. They do not read or
  compare playbook YAML, task names, modules, inventory content, or role layout.

## AN002 — users and shared access

The learner creates the `automation` group, adds `deploy` and `auditor`, creates the setgid
`/srv/automation` directory, and installs the controller-provided public key as deploy's sole
authorized key. Grading checks account memberships, directory ownership and mode, and the exact
authorized-key outcome after a target reset.

## AN101 — variables, templates, and handlers

The learner renders the specified exact chrony configuration and keeps `chronyd` enabled and
active. Grading runs the playbook twice, records the service PID after each run, and requires the
PID to remain unchanged. This makes an unnecessary handler restart on the unchanged second run a
measurable failure without inspecting how the playbook is written.

## AN102 — reusable web automation

The starter presents a `web` role, but grading accepts any playbook structure. The learner must
produce the specified nginx service and exact page content. Grading runs the complete playbook
twice and requires the second `ansible-playbook` recap for `target` to report `changed=0`, in
addition to checking the final service and file outcomes.
