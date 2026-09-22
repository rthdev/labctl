# Repository guide for coding agents

## Scope and priorities

`labctl` is a typed Python 3.12+ CLI for reproducible Linux learning labs on local KVM/libvirt. Preserve safety and fail-closed behaviour ahead of convenience. Treat lab definitions, setup scripts, graders, provider plugins, images, and Python build backends as potentially executable supply-chain inputs.

Keep changes focused. Add a failing regression test before changing behaviour, then make the smallest implementation change that passes it. Do not weaken validation, trust checks, ownership checks, path containment, host-key verification, timeout handling, or cleanup guarantees to make a test pass.

## Repository map

- `src/labctl/cli.py`: command-line parsing and exit behaviour.
- `src/labctl/application.py`: application-level command dispatch.
- `src/labctl/definitions.py`: definition loading, validation, snapshots, and trust.
- `src/labctl/provider.py`: provider interface and discovery.
- `src/labctl/kvm.py`: libvirt-backed provider implementation.
- `src/labctl/orchestrator.py`: lifecycle orchestration and rollback.
- `src/labctl/lifecycle.py`: lifecycle states and transitions.
- `src/labctl/state.py`, `paths.py`, and `locks.py`: persistent state, path containment, and locking.
- `src/labctl/images.py`: image metadata, import, provenance, and digest checks.
- `src/labctl/ssh.py` and `grading.py`: pinned SSH access and bounded host-side grading.
- `src/labctl/data/`: bundled schemas, lab definitions, setup scripts, graders, and image metadata.
- `tests/`: deterministic unit and contract tests; normal tests must not mutate real libvirt resources.
- `docs/`: public behaviour, architecture, security, provider, build, and platform guidance.

Read the relevant document before changing a subsystem. In particular, consult `docs/architecture.md`, `docs/security.md`, `docs/definitions.md`, `docs/providers.md`, and `docs/grading.md` for their corresponding contracts.

## Development setup

Use the repository-local virtual environment and the declared development dependencies:

```console
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[dev]'
```

Development dependencies are declared in `pyproject.toml`; no development lock
file is currently shipped. Use an isolated environment, not the system interpreter.

## Public evidence privacy

Use literal `$HOME` rather than an operator's real home-directory path in
committed documentation, test logs, reports, and shared verification artifacts.
Sanitize captured output before staging it; also remove usernames in prompts and
other identifying metadata. Keep raw evidence local and untracked. Deliberate lab
guest accounts such as `/home/student` are part of the exercise contract and must
not be rewritten. Check staged content before every publication; sanitizing the
current tree does not remove disclosures from Git history.

## Canonical verification

Run the repository's complete local quality gate from its root:

```console
scripts/verify
```

It checks Ruff formatting and lint, strict mypy, pytest, wheel and sdist builds. Run targeted tests while iterating, but run `scripts/verify` before considering a change complete. Also run `git diff --check` and inspect the final diff for unintended generated files or unrelated edits.

When changing user-visible behaviour, update the relevant documentation and tests in the same change. When changing schemas or serialized state, retain explicit version handling and add compatibility or rejection tests. Do not replace reproducible checks with machine-specific verification logs.

## Safety invariants

- A resource name is not proof of ownership. Require complete matching ownership metadata before mutation or deletion.
- Keep provider, image-digest, lab-ID, and VM-name lock ordering consistent with `docs/architecture.md`.
- Validate identifiers and contain filesystem paths before creating, replacing, or deleting files. Preserve private directory and file modes.
- Use argument vectors for subprocesses; never add shell interpolation for untrusted values.
- Preserve bounded execution, process-tree cleanup, minimal grader environments, and fail-closed error handling.
- Preserve pinned SSH host-key verification. Never substitute disabled checking, `/dev/null`, or unauthenticated `ssh-keyscan` bootstrapping.
- Preserve image digest and provenance checks. Transport security alone is not provenance.
- External definitions and providers require explicit trust and source review. Provider imports execute with the user's privileges.
- Creation, reset, reconciliation, and cleanup must remain retryable without adopting or deleting foreign resources.
- Do not silently broaden supported platforms or claim real-host compatibility from mocked tests.

Changes in these areas need negative tests that demonstrate rejection of unsafe, malformed, foreign, or partially failed states.

## Real libvirt operations

The normal test suite uses fakes and must not access or mutate real libvirt resources. Do not run `scripts/real-libvirt-smoke` unless the task explicitly requires real-host testing and the operator has provided an expendable, authorised environment, a reviewed local image, an independently trusted lowercase SHA-256 digest, and the required consent variables.

Follow `docs/platform-verification.md` exactly. Never enable the real-host harness in CI, point its temporary XDG roots at operator state, run it concurrently, or assume cleanup succeeded after an error. Retain and report recovery state when cleanup fails.

## Packaging and releases

Build with:

```console
.venv/bin/python -m build --no-isolation --sdist --wheel
```

Package labctl only as Python wheels and source distributions. Keep project
metadata, `LICENSE`, bundled package data, and documentation consistent. Follow
`docs/building.md` for build guidance. Do not run unreviewed source trees or
Python build backends with privileged tooling.
