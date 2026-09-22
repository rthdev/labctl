# Platform Verification

Static platform support recognizes RHEL, CentOS Stream, and Rocky Linux 9/10,
Fedora 44, and Arch. RPM-family guidance installs `qemu-kvm`, `libvirt`,
`virt-install`, `libvirt-client`, and `cloud-utils`; Arch guidance installs
`qemu-full`, `libvirt`, `virt-install`, `dnsmasq`, and `cloud-image-utils`.

Automated tests use deterministic command fakes for provider and bundled grader
behavior. They verify parsing, package guidance, capability selection, strict
SSH arguments, schema loading, and pass/fail grading decisions.

`labctl provider doctor` reports every check for the local selected provider,
but does not operate a fleet. Run it independently on all hosts. Before claiming
a host verified, an operator must complete all checks:

1. Confirm `/dev/kvm` is usable by the invoking user.
2. Confirm `virsh`, `qemu-img`, `virt-install`, `cloud-localds`, and `ssh` resolve.
3. Probe libvirt compute capabilities, storage pools, and networks on both the
   selected URI and any fallback URI.
4. Confirm the dedicated storage root is owned and usable by the invoking user,
   and separately verify QEMU DAC access and SELinux labelling. Do not infer QEMU
   access from the doctor current-user check.
5. Confirm isolated NAT DHCP and outbound DNS without exposing guest services.
6. Import a provenance-recorded qcow2 and verify its digest and structure.
7. Create, start, SSH with a pinned host key, stop, reset, and delete each lab.
8. Run setup twice, confirm pristine state remains unsolved, solve manually, run
   the grader, and confirm a second Ansible run reports zero changes.
9. Exercise rollback, timeout, drift, foreign ownership, lock conflict, and
   state migration recovery paths.

A Fedora 44 nested-KVM verification has additionally exercised package
installation, system-libvirt modular sockets, SELinux enforcing mode, managed
image download, isolated NAT, Rocky 9 guest creation and SSH, grading,
stop/start/restart, reset, active-domain reconciliation, and complete removal.
Nested virtualization does not prove bare-metal firmware or site-specific access
policy. Historical distro-specific scope and limitations are documented in
the [Arch report](arch-verification.md) and
[CentOS Stream 10 report](centos-stream-10-verification.md). Mocked matrix tests
alone are not real-host compatibility evidence.
