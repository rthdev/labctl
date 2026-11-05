# Rocky Linux 10 operational curriculum

This additional track contains exactly **15 independent labs**, with three labs
at each difficulty level. Existing Rocky Linux 9, Ansible and container labs are
retained unchanged. All assignments, hints and grader messages are in English.
No lab requires another lab's VM, files or prior completion.

## Official objective basis

The curriculum uses the publicly listed objectives from:

- [EX200: Red Hat Certified System Administrator](https://www.redhat.com/en/services/training/ex200-red-hat-certified-system-administrator-rhcsa-exam)
- [EX342: Red Hat enterprise Linux diagnostics and troubleshooting](https://www.redhat.com/en/services/training/ex342-red-hat-certified-specialist-linux-diagnostics-and-troubleshooting-exam)

These pages were consulted for this curriculum. Red Hat can change objectives
and version-specific exam requirements. Rocky Linux 10 is the practice guest,
not a claim of Red Hat certification equivalence. This is **not a complete exam preparation**
course, an official Red Hat product, or a reproduction of exam questions.

| Lab | Level | Title | Selected official objective areas | Observable result |
| --- | --- | --- | --- | --- |
| LX010 | 0 — Fundamentals | Shell navigation and file operations | EX200: files, directories, editing, essential shell tools | Required workspace, copied/moved files and exact content exist with the assigned identities. |
| LX011 | 0 — Fundamentals | Text filtering and shell pipelines | EX200: redirection, grep, regular expressions, text processing | Reports match supplied source records and required output formats. |
| LX012 | 0 — Fundamentals | Permissions, links, and compressed archives | EX200: permissions, hard/soft links, tar/compression | Access modes, link identity and archive/restored content satisfy the assignment. |
| LX110 | 1 — Beginner | Local accounts and scoped privilege | EX200: users/groups, password aging, privileged access | Account attributes, group membership and scoped sudo access work without broad privilege. |
| LX111 | 1 — Beginner | Packages, service lifecycle, and journal evidence | EX200: RPM installation, systemd, persistent journals | Installed package, enabled/running service and persistent current service evidence agree. |
| LX112 | 1 — Beginner | Safe dual-stack NetworkManager practice | EX200: IPv4/IPv6, hostname resolution, persistent networking | An isolated dummy interface has the specified dual-stack state without replacing management networking. |
| LX210 | 2 — Intermediate | Persistent LVM records volume | EX200: PV/VG/LV creation, XFS, persistent mounts | The specified LV on the supplied medium is mounted with retained content after reboot. |
| LX211 | 2 — Intermediate | SELinux and firewall service repair | EX200: file/port labels, firewalld, enabled services | Confined Apache serves the required tree through persistent ingress policy after reboot. |
| LX212 | 2 — Intermediate | Repair a scheduled shell report | EX200: conditional scripts, loops/input handling, systemd timers | A scheduled report processes fresh input correctly and rejects stale output after reboot. |
| LX310 | 3 — Advanced | Boot-path and persistent filesystem recovery | EX342: boot/service recovery, root control, filesystem diagnosis; EX200: bootloader and SELinux | Normal boot, recovered root access, persistent original filesystem and security labels are restored. |
| LX311 | 3 — Advanced | Network-to-storage incident diagnosis | EX342: network verification, traffic diagnosis, filesystem/application failures | Remote service reachability and a fresh write to the original medium both work after reboot. |
| LX312 | 3 — Advanced | Application failure and memory diagnosis | EX342: application debugging, resource monitoring; EX200: process/service management | The intended unprivileged process serves its dataset within bounded effective memory limits. |
| LX410 | 4 — Expert | Account admission and application access incident | EX342: PAM/account policies, application diagnosis | Scoped account admission, negative access canaries and the dependent API all satisfy policy. |
| LX411 | 4 — Expert | Encrypted ledger recovery across storage layers | EX342: encrypted data and LVM recovery, startup dependencies | Original encrypted-media/device chain, preserved ledger and dependent service work after reboot. |
| LX412 | 4 — Expert | Correlated application outage and memory retention | EX342: application/memory investigation, changed files, SELinux, diagnostic evidence | The intended proxy/backend chain, bounded memory behavior and fresh diagnostic evidence agree. |

## Progression

- **Level 0:** explicit paths and outputs; guided local operations without service incidents.
- **Level 1:** routine administration combining persistent configuration with effective state.
- **Level 2:** combined subsystems; bounded faults and functional tests rather than file presence.
- **Level 3:** several independent failure layers; diagnose the causal path, recover it and prove persistence.
- **Level 4:** dependent service and security/storage failures; negative controls, data preservation and sustainable recovery matter together.

The ID scheme is `LX<level>1<index>`: level is 0–4, the middle digit `1`
identifies this Rocky 10 track, and index is 0–2. The existing Rocky 9 track keeps
its original identifiers; it is not silently migrated.

## Running a lab

Install from the reviewed repository or its wheel, not the unrelated public
PyPI package with the same name. Prepare the host as described in the existing
installation guide, then:

```console
labctl image pull rocky:10
labctl lab inspect LX010
labctl lab create LX010
labctl lab ssh LX010
```

Read `/home/student/LAB.md` in the guest. The canonical assignment is the bundled
`lab.yaml` instructions body, delivered by provisioning. Grade from the host:

```console
labctl grade LX010
```

Use the exact reboot and console requirements in each assignment. In particular,
LX310 deliberately stages future boot faults: successful creation is not proof
that the next boot is healthy. Open the console before activating those faults.
A final-state grader cannot prove that a human performed console recovery;
that acceptance path requires observed faulty boot and console repair evidence.

## Reproducibility and boundaries

- Every lab uses `rocky:10`; the image metadata records a specific build and
  checksum. Provisioning requires access to the configured Rocky repositories.
  Guest package repositories are not historical, immutable package snapshots.
- Setup stages exercise inputs and faults, not completed learner outcomes.
  Repeated setup must not destroy an existing attempt; reset/recreation is the
  explicit way to discard learner work.
- Storage exercises use bounded image files inside their disposable guests,
  because the current definition schema has no additional-disk field. The
  mounted/LVM/encrypted source must resolve to the supplied medium.
- Management SSH remains available during ordinary provisioning. Security and
  resource faults are confined to the exercise, never the host engine or host
  filesystem. Do not disable SELinux/firewalld or modify protected baselines to
  make a grader pass.
- Graders check results rather than a preferred command sequence. Each semantic
  check prints PASS or FAIL. Negative command statuses are interpreted explicitly.
  Safety baselines can already pass; they are not unfinished learner objectives.
- Functional probes may create temporary guest evidence, trigger a service or
  invoke the learner's script as stated in the assignment. Grading is not
  necessarily read-only. Fresh probe data prevents stale success from passing.
- These educational graders are not a hostile-root security boundary. Readiness,
  data integrity, causal linkage and persistence checks do not make guest evidence
  impossible for a root-capable learner to tamper with.
- Installed instances retain immutable definition snapshots. Updating this
  catalogue does not retrofit assignments or graders into existing instances.

## Coverage limits and acceptance

The mappings above are selected competencies, not full objective coverage.
Examples not comprehensively covered here include Flatpak, NFS/autofs, time
clients, AIDE, the RHEL web console, centralized logging, RPM database corruption,
kernel module faults and actual kernel crash-dump generation. Diagnostic tools
may support an exercise without their invocation being a graded objective.

Definition/fake-SSH tests and sandboxed command tests are distinct from actual
Rocky 10 guest acceptance. A release acceptance run must record creation,
untouched per-check results, legitimate solution, repeated grading, required
reboot/console recovery and ownership-checked cleanup for the tested artifact.
Acceptance was completed on 2026-11-05 using Rocky Linux 10.2 guests under
nested KVM on a Fedora 44 host. All 15 labs passed creation/readiness, reviewed
untouched per-check failure, a legitimate solution, solved grading, a subsequent
real guest reboot, post-reboot grading and ownership-checked cleanup.

| Level | Accepted labs | Result |
| --- | --- | --- |
| 0 | LX010, LX011, LX012 | PASS |
| 1 | LX110, LX111, LX112 | PASS |
| 2 | LX210, LX211, LX212 | PASS |
| 3 | LX310, LX311, LX312 | PASS |
| 4 | LX410, LX411, LX412 | PASS |

LX310 additionally exercised the actual failed boot, locked-root emergency
console, visible serial GRUB menu and editing, `rd.break` recovery, credential
replacement without recording a password, preservation of the separate boot
filesystem and BLS entries, SELinux relabelling and normal boot. This was an
observed console repair, not an SSH-only preboot solution.

The acceptance evidence spans successive isolated candidate wheels. Changed
labs were retested; every accepted lab's complete packaged assets and all shared
runtime/package metadata were compared byte-for-byte with the final candidate.
Changes in other labs and correctly regenerated RECORD hashes were accounted
for separately. This is carried, byte-equivalent per-lab evidence, **not** a claim
that all 15 were freshly rerun on a single wheel. The final guest-tested 0.5.1
precursor candidate's wheel SHA-256 is
`910cd90218ae9825809f596b57e7a984f667a06131a07df2693ba75087be32ba`.
The 0.6.0 publication rebuild retains the same lab assets and runtime code apart
from the version declaration. Its version and README metadata changes and
regenerated RECORD are verified separately; it is not a fresh 0.6.0 guest run.

Local verification passed 1885 tests with one skip, plus Ruff, mypy and wheel/sdist
builds. Real-guest checks do not establish a BIOS/UEFI firmware matrix, host-reboot
recovery, bare-metal certification or complete EX200/EX342 coverage. Existing
Ansible, container and Rocky 9 labs were retained and covered by the local suite;
their real-guest acceptance was not repeated for this additive track.
