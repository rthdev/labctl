# Grading Protocol

The immutable instance snapshot supplies the host-side grader. `labctl lab grade ID`
and its shorthand `labctl grade ID` default to **current-state** mode: no disk reset,
no implicit VM startup, and no silent drift repair. Start stopped VMs explicitly
with `labctl lab start ID` first. In Ansible labs the existing grader still executes
the learner playbook and checks its outcomes; current-state does not mean read-only.

Use `labctl lab grade ID --reset` for **clean-baseline** grading. The immutable
snapshot must declare `grading.reset_vms`; otherwise the option is rejected, never
expanded to a full reset. The prompt names exactly which target disks will lose
their work, defaults to No (`[y/N]`), and precedes all reset/start/grader activity
and progress animation. `--yes` bypasses only confirmation, not validation or scope;
it does not enable reset on its own. Noninteractive input, `--json`, and `--quiet`
require `--yes` with `--reset` and never prompt. Selected VMs are transactionally
rebuilt from the original image and setup seed, then preserved VMs are started
without replacing their disks. The grading context uses refreshed addresses.
Bundled AN labs select only managed targets, preserving controller projects and
refreshing practice access. Bundled LX and CT labs have no grading reset targets.

One per-lab lifecycle lock covers drift and snapshot validation, confirmation,
selective reset, startup, context construction, and grader completion. Both modes
refuse pending transactions, including interrupted resets: follow the reported
explicit recovery command before grading. This avoids hidden destructive recovery
before consent. Reset recovery retains its journaled selection and never widens it.

Human results identify the mode; current-state results for reset-capable labs
suggest `--reset`. JSON adds `mode` (`current-state` or `clean-baseline`),
`reset_vms` (the selected list, empty for current-state), `reset_available`, and
`lab_id` to grade results, including failed-grade `error.details`. Interactive
human output shows a spinner, elapsed time, and actual available stages (reset,
startup, and running the grader). There is no spinner for JSON, quiet, redirected,
or debug output; errors and interrupts clean it up. Playbook execution and outcome
checks remain one opaque grader phase, not fabricated separate progress stages.

The runner creates a mode 0700
temporary directory and mode 0600 schema-v1 JSON context, and passes its path as
the grader's first argument. The process receives only `LABCTL_CONTEXT`,
`LABCTL_SCHEMA_VERSION=1`, and absolute `LABCTL_SSH`, has no stdin, and runs in a
new process group. On Linux, labctl enables child-subreaper tracking, kills and
reaps adopted descendants including processes that escape with `setsid`, and
fails closed before launch when subreaper or `/proc` child tracking is
unavailable. No systemd or cgroup setup is required. Stdout and stderr are
captured in temporary files and only the first 1 MiB of each stream is loaded
into memory.
Exceeding either per-stream limit is an explicit grading error. The context and
output files are always removed.

Each host contains `name`, `hostname`, `address`, `ssh_user`,
`private_key_path`, `known_hosts_path`, `provider`, lifetime-bound
`provider_uri`, and `lifecycle_state`. Lab ID, title, goal, provider, and URI are
also included. Key paths are present, never key contents. See
[`schemas/grading-context-v1.schema.json`](schemas/grading-context-v1.schema.json).

Every stdout line is an ordered learner check result; stderr is diagnostic.
Exit 0 passes; nonzero maps to CLI grading status 7, with timeout distinguished.
Bundled graders use fixed inspection commands, explicit identities,
`StrictHostKeyChecking=yes`, and per-instance known-hosts. They never use
`ssh-keyscan` or trust-on-first-use.

AN001 preserves the controller and learner-authored `site.yml`; only `--reset`
resets the target. In either mode its grader generates a dedicated key on the controller, obtains
only the public key, installs that public key on the host-key-pinned target, and
transfers pinned target host-key data plus a dynamic inventory to the controller.
It executes `/usr/bin/ansible-playbook` there and independently checks nginx and
the exact target file state. It never copies the host management private key and
never parses or compares learner playbook or inventory content. Temporary grade
inventory and host-key files are removed after the run.
