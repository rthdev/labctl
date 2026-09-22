# Provider Authoring and Trust

Providers implement the version-1 `labctl.provider.Provider` abstract interface:
stable `id`, `api_version`, capability reporting, full doctor results, ownership
inspection/reconciliation, and lifecycle services supplied by their backend.
KVM uses `KVMOrchestrator`; future providers can compose another orchestrator
without changing CLI parsing or definition loading.

Schema-v1 creation requires the selected provider to match the definition.
Provider discovery and doctor remain extensible, but this release implements
lifecycle orchestration only for the built-in `kvm` provider; a discovered
non-KVM provider fails before images or resources are changed.

A module exports `PROVIDER_API_VERSION = 1` and one `PROVIDER` instance. Files in
`$XDG_DATA_HOME/labctl/providers/NAME.py` and configured `provider_paths` are
discovery candidates, but placement never imports them. Add `NAME` to
`enabled_providers` only after reviewing its source and dependencies. Imports
execute in-process with all user privileges. Loading errors affect commands that
resolve providers, and duplicate identities are rejected rather than selected.

Provider implementations must use argument vectors, never a shell, persist a
lifetime URI/backend binding, put ownership metadata on every resource, verify
that metadata before mutation, and return state-only reconciliation plans.

The built-in KVM provider receives the effective dedicated storage root for
doctor diagnostics. Doctor verifies only that the directory exists and the
invoking user can read, write, and search it; that does not prove that system
QEMU passes DAC or SELinux checks. Administrator provisioning must provide both.
New KVM state binds the storage path for its lifetime. Legacy state without that
field continues to use the historical private instance tree.
