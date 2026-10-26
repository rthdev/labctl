# CT UBI 9.8 migration verification

## Scope

Real nested-KVM acceptance used a CentOS Stream 10 test host, Rocky Linux 9
learner guests, rootless Podman and enforcing SELinux. The tested guest Podman
reported 5.8.2. This is not a bare-metal RHEL certification or an outer-host
reboot test. The ordinary automated suite remains separate from guest acceptance.

The catalogue uses mutable minor tags, not digest pins:

- `registry.access.redhat.com/ubi9/ubi:9.8`
- `registry.access.redhat.com/ubi9/httpd-24:9.8`

Provisioning records resolved image IDs/repository digests. Future pulls can
produce different bytes and require renewed acceptance.

## Artifacts and evidence continuity

Final tested `labctl-0.4.2-py3-none-any.whl` SHA-256:

`7e4fd0c3246538453c43671384b46b23f7a5f2d770badbfa3fb042c995eb72d4`

Earlier seven-lab tested wheel SHA-256:

`3da410c473b36aa5d63703db9e47f812dd3f681ba859e425bc9f1fb25303f671`

Complete wheel comparison found identical member names and modes. Only
`CT302/setup.sh`, `CT302/grade.py`, distribution `RECORD`, and the recorded
Hatchling generator version in `WHEEL` differ. All package-source bytes match
the final working tree. Therefore the seven unaffected labs carry prior
**equivalent-code evidence**, not fresh final-wheel runs. Documentation updates
after acceptance do not change the tested package source.

## Acceptance matrix

Every row covers fresh create/readiness, pristine grading twice, an offline
solution, solved grading twice, VM stop/start, restart, reset to pristine failure,
ownership-checked removal and baseline/lock verification. Ordinary containers
were resumed without recreation after VM start; only CT401 promises automatic
Quadlet startup. HTTP readiness was checked before grading resumed web services.

| Lab | Result | Evidence qualification |
| --- | --- | --- |
| CT001 | PASS | Prior equivalent-code run; successful one-shot container not rerun |
| CT002 | PASS | Prior equivalent-code run |
| CT101 | PASS | Prior equivalent-code run |
| CT102 | PASS | Prior equivalent-code run |
| CT201 | PASS | Prior equivalent-code run |
| CT202 | PASS | Prior equivalent-code run |
| CT301 | PASS | Prior equivalent-code run with preserved readiness failure/retries |
| CT302 | PASS | Fresh final-wheel run; CPU limit and effective hardening verified |
| CT401 | PASS | Final-wheel run; actual guest reboot and Quadlet autostart verified |
| CT402 | PASS | Fresh final-wheel run; staged mismatch, recovery and volume preservation |

CT401's kernel boot ID changed across the explicit guest reboot. No manual
service start was used after reboot, VM start or restart.

## Failures retained and corrected

- CT301's initial after-start grade ran before delayed Apache startup completed.
  An unchanged-state retry and repeat passed. Bounded read-only HTTP readiness
  corrected the harness; the original failed grade remains part of the evidence.
- CT302 first exposed missing CPU delegation in Rocky's student user manager.
  Provisioning now delegates `cpu memory pids` to that user instance. A fresh
  final-wheel guest ran the unprivileged solution with `cpu.max = 50000 100000`.
- CT302 then exposed Podman's `null` representation of empty capability sets.
  The grader accepts explicit `null` or `[]`, rejects missing/malformed/nonempty
  values, and still checks the actual PID-1 capability masks and hardening.
- CT401's foreground test transport timed out after successful creation. The
  same ready instance was continued with durable logging; no recreation or
  saved-definition alteration occurred. This was not an uninterrupted harness run.

Independent review also identified an Apache stale-loaded-configuration false
pass. Real extracted Apache reproduced it before correction; regression probes
reject it afterward. The conservative timestamp/full-restart contract and its
limitations are documented in [container labs](container-labs.md).

## Local gates and cleanup

The final implementation gate passed Ruff, mypy, **1338 tests with 1 skipped**,
wheel/sdist builds and `git diff --check`. These counts are historical local
results, not a claim about a future CI run. Independent reviews covered the
migration, Apache correction, CPU delegation and capability compatibility.

Owned guest domains, networks, storage, state and journals were removed through
the CLI; locks were released. Exact baseline checks preserved unrelated pool
XML, operator state and the original cached guest image. The isolated candidate
environment was removed after allowlisted evidence export, and the outer test
VM was restored to its initial shut-off state. Raw diagnostics remain private;
no keys, seed media or provisioning state are published here.
