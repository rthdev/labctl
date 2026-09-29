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
instructions: |
  ## Required outcomes
  Work on node. Create the `labops` group and the `opsadmin` user, with
  `opsadmin` explicitly included in the `labops` group membership list.
  Create `/srv/labshare` owned by `opsadmin:labops`, mode `2770`, and
  `/srv/labshare/operations.txt` with the same owner and group, mode `0660`.
  Preserve the directory's setgid bit.

  ## Validation
  Run `labctl lab grade LX001` on the host, not inside the VM.
provider: kvm
grader: grade.py
# Optional: permits selective reset only when grade --reset is requested.
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
transactionally reset before the grader starts only with `grade --reset`. Every
entry must reference a VM in the same definition. Default grading never resets
VMs. Omitting `grading` makes `grade --reset` unavailable; lab IDs and prefixes
do not enable it. Confirmation (or `--yes`) is also required for reset grading.
Only selected domains, overlays, addresses, cleanup, rollback, recovery, and
power state participate in the reset transaction. If a selective reset is
interrupted, recovery retains the journaled selection: a later unqualified
`lab reset` first rolls back and then repeats only that selection rather than
widening the operation to every VM.

## Learner instructions

From 0.4.0, `instructions` is the canonical, self-contained Markdown assignment.
Use a YAML literal block (`|`) to preserve paragraphs, lists and fenced examples.
Keep `title` as a short display name and `goal` as a one-sentence learning
objective; neither replaces the assignment.

Document the starting state, required observable outcomes, exact paths and
names, expected content, permissions, persistence/reboot requirements, and
forbidden shortcuts. Separate controller, target and host actions explicitly.
Include practice and validation guidance, but do not pre-complete the exercise
or require learners to read grader source to discover acceptance criteria.

The KVM provisioning layer publishes `LAB.md` in the configured SSH user's home
(default `/home/student/LAB.md`) on every VM. It adds a heading with the lab ID
and title, then preserves the `instructions` body unchanged. The file is owned
by the guest user and group with mode `0644`. Host-side `lab inspect` displays
the same assignment body. Multi-VM labs receive the same complete document so
that the assignment remains available from each VM.

Setup scripts prepare packages, files, services and staged faults only. Do not
write a second `LAB.md`, interpolate instructions into shell commands, or keep
an abbreviated copy in setup scripts. Custom setup scripts must not overwrite
the centrally delivered document. This is a provisioning convention using the
existing schema-v1 `instructions` field, not a new definition format.

Assignments are creation-time data from the saved definition. Existing labs
retain their immutable snapshots and cloud-init seeds; neither upgrading labctl
nor resetting an existing lab imports revised installed instructions. Recreate
after backing up learner work to adopt updated assignments. A manual,
document-only correction is possible, but do not rerun setup scripts to update
documentation: they can overwrite learner work or restage faults.

Tests must verify guest delivery through the actual provisioning path and
check concrete learner requirements against grader expectations. Testing only
that a setup script mentions `LAB.md` cannot detect an incomplete assignment.

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
