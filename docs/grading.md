# Grading Protocol

The immutable instance snapshot supplies the host-side grader. `labctl grade`
observes current state without silently repairing drift. If that snapshot opts
in with `grading.reset_vms`, labctl transactionally resets exactly those VMs,
starts preserved VMs without replacing their disks, and builds the grading
context from refreshed addresses. One per-lab lifecycle lock covers drift and
snapshot validation, recovery, selective reset, startup, context construction,
and grader completion, so another lifecycle command cannot change the lab between
those steps. It then creates a mode 0700
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

AN001 preserves the controller and learner-authored `site.yml` while resetting
only the target. Its grader generates a dedicated key on the controller, obtains
only the public key, installs that public key on the host-key-pinned target, and
transfers pinned target host-key data plus a dynamic inventory to the controller.
It executes `/usr/bin/ansible-playbook` there and independently checks nginx and
the exact target file state. It never copies the host management private key and
never parses or compares learner playbook or inventory content. Temporary grade
inventory and host-key files are removed after the run.
