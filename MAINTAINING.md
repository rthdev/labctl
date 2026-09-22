# Maintaining labctl

This is the operational entry point for maintainers and coding agents. Read the linked documentation before changing behaviour; distro verification reports in `docs/` are historical evidence, not current test results.

## Project constraints

`labctl` is a Python 3.12+ CLI for reproducible Linux learning labs on local KVM/libvirt. It treats definitions, images, state, provider resources, and grader output as untrusted inputs. Safety and recoverability take priority over convenience.

Do not weaken these invariants:

- Never identify ownership from a libvirt resource name alone. Verify all ownership metadata described in `docs/architecture.md` before mutation or deletion.
- Keep create, reset, remove, and reconcile crash-safe and retryable. Persist and fsync state before external mutations where the transaction design requires it.
- Do not follow symlinks for state, locks, images, definitions, scripts, or managed VM files.
- Keep subprocess invocation argument-based (`shell=False`). Never interpolate untrusted values into a shell command.
- External definitions/providers and unverified images must remain opt-in and fail closed.
- Graders run as bounded, isolated host processes. Preserve time, output, environment, process-tree, and schema limits.
- JSON output and exit statuses are public API contracts. Keep stdout machine-readable in JSON mode and route errors consistently.
- Unit tests must not mutate real libvirt. Real-host tests stay behind the explicit `LABCTL_REAL_LIBVIRT=1` gate.

## Repository map

- `src/labctl/cli.py`: parser, aliases, output envelopes, and exit mapping.
- `src/labctl/application.py`: command dispatch and user-facing operations.
- `src/labctl/orchestrator.py`: stateful lifecycle orchestration and recovery journals.
- `src/labctl/provider.py`: provider interfaces, discovery, health, ownership, and reconciliation types.
- `src/labctl/kvm.py`: built-in libvirt/KVM provider.
- `src/labctl/definitions.py`: strict definition parsing, discovery, and digests.
- `src/labctl/images.py`: image validation, provenance, import, pull, and content-addressed storage.
- `src/labctl/state.py`, `locks.py`, `paths.py`, `trust.py`: durable local state and filesystem security.
- `src/labctl/lifecycle.py`: dependency order, transitions, rollback, cleanup, and generic reconciliation.
- `src/labctl/grading.py`: bounded grader execution and containment.
- `src/labctl/cloudinit.py`, `ssh.py`, `subprocesses.py`, `platform.py`, `config.py`: supporting boundaries.
- `src/labctl/data/labs/`: bundled definitions, setup scripts, and graders.
- `src/labctl/data/images/metadata.json`: managed-image provenance catalog.
- `docs/schemas/`: versioned public JSON and definition schemas.
- `tests/`: unit, contract, package, bundled-lab, and gated real-libvirt tests.

Start with `README.md`, `docs/architecture.md`, and `docs/security.md`. Read the topic-specific document before changing CLI, definitions, providers, images, grading, configuration, or packaging.

## Development setup

Use an isolated Python 3.12+ environment:

```console
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
```

Do not install dependencies into the system interpreter. The project intentionally supports standard `venv`/`pip`; do not introduce another environment manager without a repository-wide decision.

## Canonical verification

Run the complete non-destructive gate before committing:

```console
PYTHON=.venv/bin/python scripts/verify
```

It checks formatting, lint, strict typing, the full default pytest suite, and both Python distribution artifacts. The default suite skips real-libvirt work.

For focused iteration, run the narrowest relevant tests first, then the complete gate:

- CLI/output contract: `tests/test_cli.py tests/test_contract.py tests/test_application.py`
- Lifecycle/resource safety: `tests/test_lifecycle.py tests/test_orchestrator.py`
- Provider/KVM/platform: `tests/test_provider.py`
- Definitions/bundled content: `tests/test_definitions.py tests/test_bundled_labs.py`
- Images: `tests/test_images.py`
- Grading: `tests/test_grading.py`
- State, paths, locks, config: `tests/test_state.py tests/test_locks.py tests/test_config.py`
- Packaging: `tests/test_package.py`

Add a regression test that fails before every bug fix. For security or lifecycle changes, test malformed input, partial failure, retry/recovery, ownership conflict, and absence/probe-error distinctions where applicable.

## Cross-file change checklist

Update all affected surfaces in the same change:

- CLI syntax/aliases/output/exit status: parser, application dispatch, `docs/cli.md`, JSON schema if applicable, and contract tests.
- Definition fields: parser/dataclasses, `docs/definitions.md`, `docs/schemas/lab-v1.schema.json`, bundled definitions, and tests.
- State/journal shape: validation/migration, recovery paths, architecture docs, and crash/retry tests. Never silently accept a future schema version.
- Provider API: interface/version checks, discovery, built-in KVM provider, `docs/providers.md`, and fake-provider tests.
- Image metadata: parser, `metadata.json`, provenance docs, and package-data tests. Never add a URL without an independently verified digest.
- Grading context/result: implementation, grading schemas, docs, bundled graders, and containment tests.
- Package version: `pyproject.toml`, `src/labctl/__init__.py`, examples, and expected artifact names.
- Dependencies/packaged data: `pyproject.toml`, build docs, and wheel/sdist verification.

## Real-host testing

Do not set `LABCTL_REAL_LIBVIRT=1` merely to make a test run. First satisfy every prerequisite in `docs/platform-verification.md`, use disposable resources, and inspect the gated test itself. Never run real-host tests in CI or on an unknown workstation. Record exact host/provider/image prerequisites and observed results when a real smoke test is intentionally performed.

## Evidence

Keep durable instructions in `README.md`, `docs/`, and this file. Keep concise
distro verification reports in `docs/`, with explicit tested versions, scope,
and limitations. Keep raw development logs and one-off audit outputs outside
the repository; use CI artifacts or release notes for appropriate sanitised
release-specific evidence. Do not reuse historical pass counts or temporary
paths as current claims. A change is complete only when current commands have
run and their actual results are reported.
