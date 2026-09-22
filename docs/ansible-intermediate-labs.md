# Intermediate and advanced Ansible labs

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
