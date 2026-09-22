# Introductory Ansible labs

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
