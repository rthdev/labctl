# Arch Linux verification — partial, blocked

This is real **Arch host** testing with **Rocky Linux 9 nested-KVM guests**.
It is not a claim that every bundled lab works on Arch or that the lab guests
run Arch. The application source tested was the `develop` checkout at
`9e25a8a`; no application or grader fixes were applied during testing.

## Environment and installation

- Official Arch cloud image: `Arch-Linux-x86_64-cloudimg-20260915.594445.qcow2`.
  Its publisher-signed checksum and downloaded bytes were verified.
- Host: x86_64, kernel `7.2.6-arch2-1`, Python `3.14.7`.
- QEMU `11.1.1-4`, libvirt `1:12.7.0-1`, virt-install `5.1.0-4`.
- cloud-image-utils `0.33-3`, python-pipx `1.15.0-1`.
- 16 GiB RAM, 8 host-passthrough vCPUs, 100 GiB sparse disk.
- System libvirt, modular daemon sockets, native nftables networking.
- Dedicated operator-owned storage, group `libvirt-qemu`, mode `2750`.
- No enforcing SELinux policy on the Arch host; Rocky guest policy was not
  disabled to obtain results.

`pipx install .` from the transferred repository succeeded. CLI help, catalogue
listing, provider doctor, and the managed `rocky:9` image pull succeeded.
The image SHA-256 was
`92c206cc6f790c61583247eefe87890f8828420662c17cacf247cec78ab4eec8`.

On Arch, `PYTHON=.venv/bin/python scripts/verify` reported **307 passed,
1 skipped**, with Ruff formatting/lint, mypy, and wheel/sdist builds passing.
The skipped test is the optional real-libvirt pytest test; the real guest
checks below were run separately. The checkout lacked `requirements-dev.lock`,
so development dependencies were installed from `.[dev]`; no hash-locked
development-environment claim is made.

## Real lab results

| Labs | Result | Verified scope |
|---|---|---|
| LX001, LX002, LX102 | PASS | Create, pinned SSH, setup rerun, pristine grading failure, legitimate solution, two successful grades, reconcile, stop/start/restart, reset, reset grading failure, removal and absence checks. |
| LX101 | FAIL | Solved grading fails its journald configuration check due to a grader quoting defect. Other solved checks and lifecycle/reset/removal succeeded. |
| LX201 | FAIL / recovery blocked | Create exceeded the test harness's 1,200-second deadline despite root reporting cloud-init complete. Unprivileged readiness failed; transaction recovery and cleanup remain outstanding. |
| LX202, LX301, LX302, LX401, LX402 | NOT RUN | Execution halted at LX201 recovery. |
| AN001, AN002, AN101, AN102, AN201, AN202, AN301, AN302, AN401, AN402 | NOT RUN | No real Ansible lifecycle, solved grading, or idempotence result is claimed. |

The matrix contains **20 labs: 3 passed, 2 failed, 15 not run**. A prepared
reference solution or a syntax check is not counted as a real guest pass.

### LX101: journald grader quoting

The solved grader returns exit `7` with:

```text
PASS LX101: httpd package is installed
PASS LX101: httpd is enabled
PASS LX101: httpd is active
PASS LX101: health endpoint responds
FAIL LX101: journald persistence is effective
PASS LX101: persistent journal storage is operating
```

Executing the exact configuration-check command from
`src/labctl/data/labs/LX101/grade.py` produces an awk syntax error. Nested shell
quoting strips required string quotes. The correct persistent configuration
must not be weakened or replaced to conceal this false failure.

### LX201: cloud-init readiness and recovery

The guest's root cloud-init status reports `done` and no errors, but a student
status command cannot traverse mode-`0700` `/run/cloud-init` and raises
`PermissionError` reading `/run/cloud-init/cloud.cfg`. The earlier unprivileged
wait remained blocked. The observation does not establish which package or
script changed that directory's mode.

The harness deadline expired. Subsequent ownership-checked removal returned:

```text
error: pending transaction for LX201 requires recovery with lab reset before removal
```

The recovery/termination operation encountered a tool-approval timeout and did
not execute. No transaction records were deleted and no direct resource deletion
was substituted. The instance remains available for diagnosis. This is not a
successful cleanup or complete catalogue verification.

## Retained test environment and evidence

The outer `labctl-arch-e2e` VM remains running. The existing Fedora test VM was
not operated on. Earlier LX001, LX002, LX101, and LX102 instances were removed;
LX201's owned domain, network, state, and storage remain:

- Instance UUID: `b397e961-c6e7-442a-b472-68c157399ab8`.
- Domain: `labctl-b397e961-c6e7-442a-b472-68c157399ab8-node`.
- Network: `labctl-b397e961-c6e7-442a-b472-68c157399ab8-network`.

Local evidence is under `$HOME/.local/state/labctl-arch-e2e/`:
`lab-matrix.json`, `ARCH_LAB_TEST_REPORT.md`, and `raw-evidence/` (including
97 command-result JSONL records). Do not share that parent directory wholesale:
it also contains outer-VM credentials. The separate evidence export excludes
private keys and cloud-init user-data.

Further work requires authorised transaction recovery, defect correction and
retesting, then the remaining catalogue. Full rollback, foreign-ownership,
lock-conflict, and migration fault injection were not exercised here. Nested KVM
does not establish bare-metal firmware or site-specific access-policy support.

See [Arch installation](../ARCH_INSTALL.md) for the tested setup commands.
