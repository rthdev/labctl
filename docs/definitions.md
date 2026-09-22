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
