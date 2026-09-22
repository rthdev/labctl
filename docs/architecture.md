# Architecture and Lifecycle

Definitions describe desired labs. Providers own compute, storage, and network
operations. Cloud-init creates a locked `student` account with a per-lab SSH
key and runs setup once. State records identity and provider URI. Graders are
separate host processes receiving a minimal protected context. Image storage is
content-addressed and independent of mutable lab disks.

Application-created KVM labs split private control material from files read by
system QEMU. `DATA/instances/ID` contains the definition snapshot, keys,
`network.xml`, cloud-init sources, and `known_hosts` under private modes.
`LIBVIRT_STORAGE_ROOT/INSTANCE_UID` contains copied content-addressed bases,
`vms/NAME/disk.qcow2`, and `seed.iso`. Overlays back onto the verified copy, so
QEMU never traverses the private XDG cache.

Shared directories are created as `2750` without a post-creation chmod, preserving
the storage root's inherited `qemu` group even when the operator is not a member
of that group. Copied distribution bases are `0444` inside that
traversal-restricted tree; overlays are `0660` and seeds are `0640`. Libvirt may
dynamically change file ownership to `qemu` while a domain is active. Labctl
fsyncs artifacts before handing them to libvirt and uses the QEMU monitor to
verify an active overlay's backing chain.

KVM attaches the prebuilt overlay and read-only seed using `virt-install
--disk none` and explicit final disk XML. The `path=` and `source.file=` disk
options trigger virtinst's implicit directory-pool registration, creating pools
without labctl ownership metadata. Creation, reset, and reset recovery avoid
that path. Fully validated historical recreation records remain accepted, but
are converted to pool-free arguments before execution. Labctl does not adopt or
delete pre-existing unmarked pools, even when their names or targets resemble
an instance; upgrading does not clean up legacy leaked pool definitions.

## Ownership

Every managed backend resource must carry all three metadata fields:
`labctl.provider`, `labctl.uid`, and `labctl.resource-type`. `labctl.uid` is an
opaque random UUID generated once and durably persisted before provisioning;
legacy persisted values remain valid. Resource names are derived from that
identity, but a name is not proof of ownership. Missing resources may be
created; present, correctly owned resources are retained; mismatched metadata
is a conflict and must never be deleted as though owned.

## Transitions

Self-transitions are no-ops. Other allowed transitions are:

| From | Allowed destinations |
| --- | --- |
| `provisioning` | `ready`, `degraded`, `failed_cleanup` |
| `ready` | `running`, `stopped`, `degraded`, `failed_cleanup` |
| `running` | `stopped`, `degraded`, `failed_cleanup` |
| `stopped` | `running`, `degraded`, `failed_cleanup` |
| `degraded` | `ready`, `running`, `stopped`, `failed_cleanup` |
| `failed_cleanup` | `degraded` |

Create is transactional and rolls completed steps back in reverse after an
error. Start follows dependencies; stop reverses them. A targeted stop also
stops all transitive dependants in reverse dependency order. Targeted removal
refuses to remove a VM while any transitive dependant remains. Shutdown requests
a graceful stop and destroys only after timeout. Reset owns a backup until
commit or rollback and restores each domain's original power on failure.
Cleanup verifies ownership and uses a durable staged journal; retries accept a
definitively absent owned resource but reject probe errors and ownership
conflicts. Failed create cleanup retains `failed_cleanup` state and the instance
files until recovery succeeds.

New dedicated storage carries `.labctl-owner.json`, binding provider `kvm`, lab
ID, instance UUID, and resource type `storage`. Before local mutation or
recursive deletion, labctl independently derives `configured root / instance
UUID`, rejects symlink components, requires current-user ownership, and requires
the exact marker. Persisted `storage_path` is never authority by itself. A root
configuration change fails closed. Cleanup orders domains and networks before
dedicated storage, dedicated storage before private control files, and state and
journals last. Pre-existing or unmarked per-instance directories are not adopted.

## Locks and State

Locks are nonblocking advisory `flock` files opened without following symlinks.
Lock and fallback runtime directories must be owned by the current user with
mode 0700; lock files must be owned regular files with mode 0600. Global
acquisition order is provider, image digest, lab ID, then VM name; acquire
lexical keys within a level. A conflict fails rather than waiting indefinitely.

JSON state is schema-versioned, written mode 0600 through an fsynced temporary
file and atomic replacement in a mode 0700 directory. Version 1 requires string
`id` and `provider_uri`. Version 0 migrates once, retaining a `.v0.bak`; an
existing backup blocks a repeated migration. Future versions fail closed.
Version 1 may contain an absolute `storage_path`; absence means the legacy
single private instance tree.

## Drift and Reconcile

Reconciliation compares desired ownership with observations and produces an
explicit plan. Dry-run returns the plan without applying it. State-only repair
rejects every action that changes provider resources. Resource drift should be
reported as create/update/delete/conflict as appropriate; foreign ownership is
always conflict. KVM grading reconciliation also checks the isolated network
configuration, domain compute and disk/network attachments, qcow2 backing file,
power state, and the trusted definition snapshot. State repair records an absent
network as already missing so cleanup is safely retryable. Reconcile is repair,
not implicit adoption.
