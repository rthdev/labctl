# labctl

`labctl` 0.4.1 is a typed Python 3.12+ CLI for reproducible Linux learning labs
on local KVM/libvirt. It validates and snapshots definitions, maintains verified
content-addressed images, creates isolated networks and QCOW2 overlays,
provisions with cloud-init, pins SSH host keys, manages lifecycle and drift, and
runs bounded host-side graders.

## Prerequisites

- Python 3.12 or newer
- KVM-capable hardware and virtualization enabled in firmware
- libvirt, `virsh`, `qemu-img`, `virt-install`, and an isolated NAT network
- OpenSSH client for grading
- Authorized access to `qemu:///system`; managed per-lab NAT bridges are not
  supported through `qemu:///session`
- An administrator-provisioned dedicated libvirt storage root with QEMU DAC and
  SELinux access; private XDG keys and state are not placed there
- `cloud-localds` and OpenSSH
- A trusted QCOW2 image. Pinned managed downloads are included for `rocky:9`,
  `rocky:10`, and `fedora:44`.

Supported host detection currently recognizes RHEL, CentOS Stream, and Rocky
Linux 9/10, Fedora 44, and rolling Arch Linux. This is code-level support, not a
claim that end-to-end guest provisioning has been run on every host. See
[platform verification](docs/platform-verification.md).

## Install

For distribution-specific host setup and pipx installation, see
[Fedora 44](FEDORA44_INSTALL.md), [Arch Linux](ARCH_INSTALL.md), or
[CentOS Stream 10](CENTOS_STREAM_10_INSTALL.md).

Create an isolated development installation with the test, lint, type-check, and
build tools:

```console
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
PYTHON=.venv/bin/python scripts/verify
```

Build artifacts without modifying the source tree's dependencies:

```console
python3.12 -m build --sdist --wheel
```

labctl is packaged only as Python wheels and source distributions. Reproducible
build guidance is in [docs/building.md](docs/building.md). Do not run an
untrusted source checkout with privileged build tooling.

## Bash completion

Print the installed project version with `labctl version`. Generate a standalone
completion script (no Python or labctl process is run when you press Tab):

```bash
labctl completion bash > labctl.bash
bash -n labctl.bash
source ./labctl.bash
```

The `source` command enables it immediately in the current Bash shell. For all
users, review the generated file, then install it:

```bash
sudo install -m 0644 labctl.bash /etc/bash_completion.d/labctl
```

Install/enable your distribution's `bash-completion` package and start a new
interactive Bash shell for automatic loading. Alternatively, keep the file in
your home directory and source it from `~/.bashrc`; no system install is needed.
Regenerate the script after upgrading labctl. It completes nested commands,
`ls`/`list`, plural aliases, and options; live image/lab/VM IDs are not queried.

## Workflow

Diagnose the selected provider and inspect bundled definitions and images:

```console
labctl provider doctor
labctl lab list
labctl lab inspect LX001
labctl image list
```

The top-level `labs`, `vms`, `images`, and `providers` commands are shortcuts for
`lab ls`, `vm ls`, `image ls`, and `provider ls`, with identical options and output.

Pull the pinned, provenance-verified image used by the bundled labs, then create
the lab:

```console
labctl image pull rocky:9
labctl lab create LX001
labctl lab ssh LX001
labctl lab grade LX001
labctl lab reconcile LX001
labctl lab reset LX001
labctl lab stop LX001
labctl lab rm LX001
```

Use `labctl image import` with an independently trusted digest when supplying a
custom or alternate image; see [docs/images.md](docs/images.md).

Creation fails before libvirt mutation unless definition, provider, image
digest, image trust, and external-definition trust checks pass. Use
`--allow-untrusted-image` or `--trust-external` only after reviewing the input.
The default dedicated root is `/var/lib/libvirt/images/labctl/<invoking UID>`;
override it only through `libvirt_storage_root` in TOML or
`LABCTL_LIBVIRT_STORAGE_ROOT`.

Bundled exercises:

- `LX001`: users, groups, ownership, and permissions on one Rocky Linux 9 node
- `AN001`: an Ansible nginx exercise with a persistent controller and a managed
  Rocky Linux 9 target
- `CT001` through `CT402`: a ten-lab rootless Podman path from a first one-shot
  container through services, storage, builds, networking, pods, hardening,
  user Quadlet persistence, and incident recovery

From 0.4.0, each newly created KVM guest receives `~/LAB.md` from the canonical
Markdown `instructions` in `lab.yaml`. The same complete assignment is available
through `labctl lab inspect`; setup scripts only prepare exercise state. Existing
instances retain their saved definitions and cloud-init seeds: upgrading or
resetting does not retrofit revised assignments. Back up learner work before
recreating a lab. See [definition authoring](docs/definitions.md#learner-instructions).

Setup scripts do not complete the exercise. Graders run on the host and use
pinned host keys. Ansible graders execute learner playbooks, so they can change
guest state in both grading modes:

```console
labctl lab grade AN001
labctl grade AN001 --reset
labctl --json grade AN001 --reset --yes
```

Default **current-state** grading keeps disks intact and does not start stopped
VMs. **Clean-baseline** (`--reset`) destroys only declared managed target disks,
preserving the controller and learner project. It names targets and asks for
confirmation (default No); noninteractive, JSON, and quiet resets require
`--yes`. `--yes` only skips confirmation, never enables reset or bypasses safety
checks. Labs without grading reset targets (including bundled LX/CT) reject it.
Human results show the mode and all checks; interactive grading shows elapsed
progress, disabled for redirected, JSON, quiet, and debug output.

The container curriculum and its supply constraints are described in
[container labs](docs/container-labs.md). Details are in
[docs/grading.md](docs/grading.md).

## Documentation

- [CLI and exit statuses](docs/cli.md)
- [Definition schema and authoring](docs/definitions.md)
- [Architecture and lifecycle](docs/architecture.md)
- [Configuration, environment, and XDG paths](docs/configuration.md)
- [Images and provenance](docs/images.md)
- [Grading protocol](docs/grading.md)
- [Rootless container curriculum](docs/container-labs.md)
- [Security and trust](docs/security.md)
- [Provider authoring and trust](docs/providers.md)
- [Troubleshooting and doctor](docs/troubleshooting.md)
- [Python builds](docs/building.md)
- [Platform verification](docs/platform-verification.md)
- [Maintainer and coding-agent guide](MAINTAINING.md)
