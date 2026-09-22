# CLI Reference

Global options may appear before or after groups and commands: `--json`,
`--quiet`, repeatable `-v`/`--verbose`, `--debug`,
`--allow-definition-override`, `--provider NAME`, and `--uri URI`. JSON and
quiet are exclusive; verbose and debug are exclusive. SSH and console reject
JSON. `list` aliases `ls`. Top-level `labs`, `vms`, `images`, and `providers`
are exact shortcuts for the corresponding singular group's `ls` command,
including its filters and output modes.
With `--json`, help at the command, group, or subcommand level is returned as a
successful `help` envelope whose data contains the rendered `text`.

```text
labctl version
labctl completion bash
labctl labs [--available|--active]
labctl vms [--lab ID]
labctl images [--cached] [--available]
labctl providers
labctl lab ls|list [--available|--active]
labctl lab create ID [--trust-external] [--allow-untrusted-image]
labctl lab inspect|start|grade ID
labctl lab stop|restart ID [--force]
labctl lab reset ID [--force]
labctl lab reconcile ID [--repair]
labctl lab rm ID [--force]
labctl lab ssh|console ID [VM]
labctl vm ls|list [--lab ID]
labctl vm inspect|start ID VM
labctl vm stop|restart ID VM [--force]
labctl vm ssh|console ID VM
labctl vm rm ID VM --force
labctl image pull NAME
labctl image import NAME PATH [--checksum SHA256]
labctl image ls|list [--cached] [--available]
labctl image inspect NAME
labctl image rm NAME [--force]
labctl provider ls|list
labctl provider inspect NAME
labctl provider doctor [NAME]
```

`--available` means definitions without state; `--active` means every recorded
instance. Reset prompts on a terminal and requires `--force` non-interactively.
Removal needs force only while running. Stop always tries graceful shutdown;
force permits destroy after timeout. Reconcile is dry-run by default; repair
changes state only and never creates, deletes, adopts, or rebinds a resource.
Stopping one VM also stops its direct and transitive dependants in reverse
dependency order. Direct VM removal refuses while any direct or transitive
dependant remains, even with `--force`; remove dependants first. Successful
direct removal leaves its declaration recorded as missing and degrades the lab.

Primary results go to stdout; diagnostics and errors go to stderr. Quiet
suppresses successful output only. JSON success is
`{"schema_version":1,"ok":true,"kind":KIND,"data":...}`. Errors are
`{"schema_version":1,"ok":false,"error":{"code":CODE,"message":MESSAGE}}`;
commands such as doctor may add `error.details` with their structured result.
Failed JSON grading emits exactly one error envelope; grader stderr remains in
`error.details` and is not written as raw text beside the JSON document.
The envelopes and command kinds are stable v1 automation APIs; human columns may
change. Kinds are `labs`, `lab`, `vms`, `vm`, `images`, `image`, `providers`,
`provider`, `doctor`, `reconciliation`, `grade`, `child`, `help`, `version`, and
`completion`. `version` prints `labctl VERSION`; its JSON data has `version`.
`completion bash` prints a complete standalone Bash script; its JSON data has
`shell` and `text`. See the README for enabling completion.

Successful `lab create` output is concise by default: it only confirms the lab
ID was created. Pass `-v`/`--verbose` to print the complete resulting lab state.
Pass `--debug` to print execution steps and external command lines to stderr as
they run, plus the complete resulting state on stdout. Debug output never
includes captured command output. JSON and quiet modes remain machine-stable and
do not emit debug traces.

Successful `vm start ID VM` and `vm stop ID VM` print only a concise
`VM ID/VM started.` or `VM ID/VM stopped.` confirmation by default. Use
`-v`/`--verbose` or `--debug` for the complete resulting state. JSON retains
its full structured result, and quiet mode suppresses successful output.

Exit statuses: 0 success, 2 usage/validation, 3 not found, 4 conflict/lock, 5
missing prerequisite, 6 operation failure, and 7 grading failure. Interactive
children preserve their status.
