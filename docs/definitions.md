# Lab Definitions

Definitions are UTF-8 YAML loaded with `yaml.safe_load`. Schema version 1 is
closed: unknown fields are errors. Script paths are relative to `lab.yaml`, must
remain inside that directory, must be executable regular files, and cannot be
symlinks. IDs match `^[A-Z]{2}[0-9]{3}$`; VM names are DNS-safe lowercase names.
Sizes are positive integers suffixed `KiB`, `MiB`, `GiB`, or `TiB`.

The machine-readable JSON Schema is
[`schemas/lab-v1.schema.json`](schemas/lab-v1.schema.json). The loader is
authoritative and additionally verifies script files and dependency cycles,
which JSON Schema alone cannot do.

## YAML Example

```yaml
schema_version: 1
id: LX001
title: Users and permissions
goal: Configure shared access.
instructions: Complete the assignment on node.
provider: kvm
grader: grade.py
# Optional and opt-in. Omit it to preserve every VM before grading.
grading:
  reset_vms: [node]
vms:
  - name: node
    cpus: 1
    ram: 1GiB
    disk: 12GiB
    image: rocky:9
    hostname: node.lab
    interfaces:
      - type: isolated_nat
    setup: setup.sh
    depends_on: []
    ssh_user: student
```

Exactly one `isolated_nat` interface is required. Every dependency must name
another VM in the same definition; dependencies determine startup order and
reverse shutdown order. `ssh_user` is optional.

`grading.reset_vms` is an optional, non-empty, duplicate-free list of VMs to
transactionally reset immediately before the grader starts. Every entry must
reference a VM in the same definition. Omitting `grading` preserves schema-v1
behavior and performs no pre-grade reset; lab IDs and prefixes do not enable it.
Only selected domains, overlays, addresses, cleanup, rollback, recovery, and
power state participate in the reset transaction. If a selective reset is
interrupted, recovery retains the journaled selection: a later unqualified
`lab reset` first rolls back and then repeats only that selection rather than
widening the operation to every VM.

## Controller practice access

Ansible KVM definitions can explicitly opt into persistent controller-to-target
practice access:

```yaml
controller_access:
  controller: controller
  targets:
    target: [web]
```

The controller and each target must name distinct declared VMs using the
`student` SSH user. Each target has a non-empty, duplicate-free list of inventory
groups, matching `[a-z][a-z0-9_]*` and excluding `all` and `ungrouped`. The loader
accepts this opt-in only for `AN*` labs with the `kvm` provider. The ID prefix
alone never enables it; definitions without the field keep their old behaviour.
Multi-node labs declare each target and its groups separately.

Once all participants are running and ready, labctl generates a dedicated key
inside the controller, authorises only its public key on targets, and writes:

- `/home/student/.ssh/labctl-practice/id_ed25519` (private key stays in guest);
- `/home/student/.ssh/labctl-practice/known_hosts` and `config`;
- a managed include at the start of `/home/student/.ssh/config`; and
- `/home/student/ansible-lab/inventory.ini`.

The SSH aliases are the target VM names. Host keys come from the pinned
provisioning state, never unauthenticated network discovery. Practice access is
separate from the grader's temporary credentials and inventory.

Creation, start, restart, and reset refresh managed connection files without
replacing playbooks or unrelated SSH settings and authorised keys. Do not edit
managed files; keep custom inventories separately. A selective operation does
not start unselected peers: if a participant is stopped or missing, state records
`controller_access_status: pending`. A subsequent start with all participants
running refreshes access and records `ready`. A failed or interrupted reset also
leaves access `pending`: disk rollback cannot undo public-key changes already
written to preserved peers. After ownership-checked recovery, run
`labctl lab start LAB_ID` to refresh access from the restored controller key.
Recovery itself does not start unselected peers or claim practice access is ready.

Existing instances use their immutable definition snapshots. Updating installed
bundled definitions does not add this opt-in to existing labs; recreate after
backing up learner work. Full reset still replaces the controller disk when the
controller is selected.

## Author and Provider Trust

Bundled definitions are application code and receive the package publisher's
trust. External definitions are untrusted executable content: review YAML,
setup, grader, and all adjacent files before accepting their directory SHA-256.
The digest covers every relative path, entry kind, symlink target, and regular
file byte.
External duplicates are rejected, and replacing a bundled ID requires explicit
override. Overrides change what code runs and are not a compatibility feature.

Provider plugins are equally privileged. Drop-ins live under
`$XDG_DATA_HOME/labctl/providers` or configured `provider_paths`, but remain
inert until named by `enabled_providers`. Each exposes `PROVIDER` and provider
API version 1. Duplicate identities fail closed. Enabling grants the plugin the
privileges of the `labctl` process.
