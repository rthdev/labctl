# Security and Trust

Treat definitions, setup scripts, graders, provider plugins, images, and Python
build backends as executable supply-chain inputs. Bundling indicates publisher
trust, not sandboxing. External definition trust is an explicit digest decision;
review before acceptance and review again whenever the digest changes. Creation
copies the accepted bytes into a private staging snapshot, verifies that the
snapshot has the accepted digest, and executes only that snapshot.

- Use `qemu:///system` for labctl's managed per-lab NAT networks. Creating their
  Linux bridges requires privileged libvirt; a successful `qemu:///session`
  `net-list` is only read access and does not prove that network creation works.
- Require complete ownership metadata before mutation or cleanup. Never infer
  ownership from a libvirt name. New instances use a random, durably persisted
  ownership UUID so equal user IDs and lab IDs in different roots do not share
  an identity.
- Keep per-lab Ed25519 private keys mode 0600 and directories mode 0700.
- Keep QEMU-visible media in the dedicated libvirt storage root. Never grant
  QEMU traversal of the XDG data/cache trees: creation copies and re-hashes each
  required base into the UUID-named storage tree before creating its overlay.
  Copied vendor bases are mode 0444 within a mode-2750 UUID tree. The file mode
  lets the operator continue checksum/reset operations after libvirt dynamic
  ownership changes; the non-world-searchable parents preserve containment.
  Per-lab overlays and seed media remain more restrictive.
- A persisted storage path or directory name is not ownership proof. Dedicated
  storage mutation requires the configured-root derivation, no symlink
  components, current-user ownership, and an exact provider/lab/UUID/type marker.
  Missing, malformed, foreign, or altered markers fail closed.
- Lifecycle IDs and VM names are schema-validated before paths or locks are
  derived. Destructive file cleanup accepts only the fixed paths beneath the
  instance directory. Persisted `virt-install` records are treated as untrusted
  state and must match the expected executable, URI, domain, network, disks, and
  numeric compute arguments before reuse. Cleanup deletes only validated fixed
  instance paths and never asks libvirt to remove all attached storage.
- Pin provisioned host keys. Do not disable checking, use `/dev/null` as
  `known_hosts`, or bootstrap trust with unauthenticated `ssh-keyscan`.
- Keep labs on isolated NAT. Definition schema v1 does not permit bridged
  interfaces.
- Run graders with minimal environment, no stdin, bounded time, captured output,
  Linux subreaper descendant tracking, and short-lived context. Fail closed if
  complete unprivileged descendant tracking is unavailable. Context contains
  key paths, never key contents.
- Verify image SHA-256 and provenance before import. HTTPS protects transport
  but does not replace release signatures or an independently trusted digest.
- Explicitly enable provider modules only after source and dependency review.

Guest setup runs as root and can change the image. Graders should normally be
read-only observers. A definition may explicitly reset selected disposable VMs
before grading; AN001 then uses fixed commands and validated data to install only
a dedicated public key and execute the learner playbook from its preserved
controller. The host management private key is never copied into a guest.
AN001 grading is pedagogical outcome validation, not adversarial anti-cheat:
because the learner playbook is granted become/root inside the guest, a malicious
learner can forge any in-guest evidence visible to the grader. Tamper-resistant
semantic verification would require a separate out-of-band or offline inspection
architecture; labctl does not inspect a running guest disk to claim that property.
An exercise VM is not a security boundary against a malicious provider or host
process.

Provision QEMU DAC access and SELinux labels only on the dedicated storage root.
Do not use `chmod 777`, disable SELinux, configure QEMU to run as the desktop
user, or expose XDG private keys and cloud-init source to solve storage access.

## Proxy credentials

Guest inheritance is an explicit ten-name proxy allowlist, never a copy of the
host process environment. Lowercase/uppercase precedence, bypass additions and
reset behavior are documented in [configuration](configuration.md#guest-proxy-inheritance).
Values are data, not shell commands: setup loads JSON into its process environment,
login shell exports are quoted, and SSH/user-systemd persistence uses the respective
literal-value formats. Unsupported control characters fail without echoing input.
labctl does not put these values in host command arguments, JSON state, transaction
journals, or debug messages. Guest setup/login failures use generic diagnostics and
capture subprocess output rather than printing potentially sensitive configuration.

Proxy credentials necessarily reach the VM: they appear in private creation
user-data (mode 0600), the restricted seed ISO (mode 0640), and guest configuration
and process environments. Guest users can read the proxy settings so their clients
and rootless user services can use them. Treat the lab's learners, root-capable
Ansible playbooks, trusted setup scripts and guest administrators as recipients of
those credentials. Use narrowly scoped credentials; guest client errors or a
learner's shell tracing can disclose them independently of labctl. Do not publish
seeds, cloud-init logs or guest configuration as diagnostic artifacts. Original
seeds retain credentials across reset; recreate to rotate them. This is not a
credential vault or a boundary against root/privileged host users.
