# Troubleshooting and Doctor

Run `labctl provider doctor` before creation. It checks the effective dedicated
libvirt storage root. Failed mandatory checks return 5;
warnings return 0. Doctor checks x86_64 and the supported OS release, `virsh`,
`qemu-img`, `virt-install`, `cloud-localds`, `ssh`, libvirt compute/storage/NAT
capabilities, current-user storage access, permissions, and SELinux mode. The
current-user check does not prove QEMU DAC or SELinux access. Doctor never
invokes package managers, sudo, ACL tools, or SELinux management commands.

RHEL, CentOS Stream, Rocky, and Fedora guidance uses `dnf install qemu-kvm
libvirt virt-install libvirt-client cloud-utils`. Arch guidance uses `pacman -S
qemu-full libvirt virt-install dnsmasq cloud-image-utils`. Run the displayed
command yourself under your site's authorization policy.

On Fedora 44, ensure all three modular sockets are active:

    sudo systemctl enable --now \
      virtqemud.socket virtnetworkd.socket virtstoraged.socket

For permission errors, verify libvirt policy and socket groups for the bound URI.
For storage/SELinux errors, have an administrator provision and label only the
dedicated libvirt storage root, then inspect AVC denials. Do not label the XDG
data/cache roots, disable SELinux, use `chmod 777`, or run QEMU as the desktop
user. For shutdown timeout, inspect guest
agents and use `--force` only when data-loss risk is accepted. For foreign
ownership or unrecoverable drift, do not rename resources to `labctl-*`; use
reset/remove when safe or perform documented manual recovery.

## Interrupted lifecycle operations

A lab left in `provisioning` can have an interrupted **create** transaction; it
is not necessarily a lab that can be reset. Recovery errors identify the pending
operation and the command that can recover it. Use the same configured state and
storage roots as the original operation.

- **Create:** run `labctl lab rm ID` to discard the incomplete lab without
  provisioning it again. Removal runs the existing ownership-checked create
  recovery under the lifecycle lock, removes owned resources and instance files,
  and removes state and the journal after cleanup succeeds. This rollback can
  stop partially provisioned VMs; it discards their data. Alternatively, retry
  `labctl lab create ID` to recover and start a fresh creation with the original
  required options (including any image-trust option). `labctl lab reset ID`
  does **not** recover create transactions.
- **Reset:** retry `labctl lab reset ID` before attempting removal or other
  lifecycle changes. Recovery retains the journal's original VM selection; a
  selective reset is not widened to a full reset. Reset replaces the selected
  VM disks and loses their changes. A committed reset only finishes its pending
  cleanup instead of resetting the disks again.
- **Lab removal:** retry `labctl lab rm ID`.
- **VM removal:** retry `labctl vm rm ID VM --force` for the VM named by the
  pending transaction, not a different VM.

If creation already committed `ready` state but its journal remains, removal
first acknowledges that commit, then performs normal lab removal. Running labs
still require stopping first or explicit `labctl lab rm ID --force`; the journal
is not permission to bypass the running-lab guard.

Cleanup failures retain recovery metadata so the operation can be retried after
fixing the underlying problem. `--force` never bypasses ownership, journal,
path-containment, or storage-marker checks. If ownership validation fails, or the
journal is invalid or names an unknown operation, retain the state/journal and
investigate the diagnostic; do not delete journals, change ownership markers,
or destroy similarly named libvirt resources to force progress. Already-absent
resources are accepted during recovery, but a provider probe error is not proof
of absence. A fully removed lab has no instance to remove again.

## Disk ownership

Fedora libvirt dynamically changes active disk, seed, and backing-file ownership
to `qemu`. This is expected. Do not recursively `chown` a running lab. New copied
bases use `0444` inside a `2750` UUID directory so the owning operator can still
verify/reset them without joining the host-wide `qemu` group; the parent
directories continue to prevent access by unrelated users.
