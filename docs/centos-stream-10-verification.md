# CentOS Stream 10 verification — historical results

This guide concerns **CentOS Stream 10 as the libvirt/application host**, not the
lab guests. The bundled labs use the managed `rocky:9` image.

A real x86_64 nested-KVM run on 21 September 2026 tested candidate
`0c868ce7ad5add4a9a006f07bcc3101871e89301`, with SELinux enforcing. All 20 labs
were attempted: 10 passed the tested sequence, AN301 passed after a readiness
retry, three had grader defects, five stopped at a confirmed readiness blocker,
and LX401 had a partial console test. **This is not an all-labs compatibility
certification**, nor does it certify later commits or bare-metal/site policy.

- LX101: nested quoting breaks the journald grader's awk command.
- AN002: invalid directory-test syntax and unprivileged checks of private files.
- AN202: unprivileged firewall queries fail even when privileged queries pass.
- LX201, LX301, LX302, AN401 and AN402: root cloud-init reports done, but the
  student readiness probe cannot read `/run/cloud-init/cloud.cfg` through a
  root-owned mode-0700 directory. Do not loosen permissions to hide this.
- AN301: selective-reset address readiness timed out once; subsequent grading
  and an independent zero-change Ansible rerun passed.
- LX401: creation succeeded, but fresh SSH closed. The console confirmed a
  faulty boot waiting for the nonexistent UUID. Console repair and solved
  verification were not tested.
- That candidate's removal left stale libvirt directory-pool definitions after
  removing the actual domains, networks and storage. Pending create transactions
  also required ownership-checked private recovery, not successful public-CLI
  recovery. Do not manually erase transaction journals or delete pools by name.

The separate target quality gate passed Ruff, mypy, wheel/sdist builds and
**307 tests, with 1 skipped**. These are not substitutes for guest testing.
Local acceptance artifacts are in
`$HOME/.local/state/labctl-cs10-e2e/`: `lab-matrix.json`,
`CENTOS_STREAM_10_LAB_TEST_REPORT.md`, and `raw-evidence/`. Those local files are
not distributed with this repository. The key-excluded ZIP is the shareable
artifact; the parent directory also contains private provisioning material.

The tested host used kernel `6.12.0-267.el10.x86_64`,
`qemu-kvm-10.1.0-28.el10`, `libvirt-daemon-kvm-12.5.0-4.el10`,
`virt-install-5.1.0-2.el10`, and `pipx-1.15.0-2.el10_3`.

### Subsequent cleanup regression check

The cleanup change following that campaign prevents virt-install from registering
implicit directory pools: it attaches prebuilt media through final domain XML.
It does not delete unmarked legacy pools. A real CentOS Stream 10 LX001
create → pristine grade → reset → pristine grade → remove check passed, with
**zero new pools** and all 34 pre-existing pool definitions unchanged. Domain,
network, state and disk-storage absence were verified after removal. The local
quality gate passed **335 tests, 1 skipped**, plus Ruff, mypy and package builds.
This focused regression is not a rerun of the full 20-lab matrix and does not
resolve its unrelated grader/readiness failures.

