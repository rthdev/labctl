# Intermediate and advanced Ansible labs

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

These labs keep the learner's `site.yml` on a persistent controller while resetting every managed target immediately before grading. The controller includes `ansible-core`; grading creates a dynamic inventory from refreshed target addresses.

| Lab | Topology | Outcome |
| --- | --- | --- |
| AN201 | app, proxy, controller | An nginx application tier is reachable end to end through a separate nginx reverse proxy. |
| AN202 | target, controller | A loop-backed ext4 filesystem is persistently mounted and serves HTTP content while firewalld is configured and SELinux remains enforcing. |
| AN301 | web1, web2, web3, controller | All three nginx targets serve release `3.1.0`; a mandatory second playbook run reports `changed=0` for every target. The lab teaches `serial: 1`, but grading checks outcomes and idempotency rather than claiming to prove execution order. |
| AN302 | target, controller | Dedicated accounts, a group-readable secret, and an exact single-command sudo rule implement a narrow privilege boundary. |

## Grading security model

Each grader:

- generates and retains a lab-specific SSH key on the controller;
- installs only its public key on each disposable target through host-pinned management SSH;
- stages a mode-0700 dynamic inventory and pinned `known_hosts` on the controller;
- never copies the management private key to a guest;
- runs `/home/student/ansible-lab/site.yml` without reading or comparing its YAML;
- checks final state directly over host-pinned management SSH; and
- removes temporary controller staging in a `finally` block.

AN302 compares the secret's SHA-256 digest and never puts its plaintext in grader commands, temporary grading files, or grader output.
