# Configuration and Paths

Precedence is CLI value, environment, TOML file, then default. Unknown TOML or
CLI keys fail. Provider, URI, output verbosity, and override CLI values are
wired into dispatch; other settings use environment or TOML.

| TOML key | Environment | Default |
| --- | --- | --- |
| `provider` | `LABCTL_PROVIDER` | `kvm` |
| `libvirt_uri` | `LABCTL_LIBVIRT_URI` | unset |
| `libvirt_storage_root` | `LABCTL_LIBVIRT_STORAGE_ROOT` | `/var/lib/libvirt/images/labctl/<invoking UID>` |
| `shutdown_timeout` | `LABCTL_SHUTDOWN_TIMEOUT` | `60` |
| `address_timeout` | `LABCTL_ADDRESS_TIMEOUT` | `120` |
| `ssh_timeout` | `LABCTL_SSH_TIMEOUT` | `180` |
| `cloud_init_timeout` | `LABCTL_CLOUD_INIT_TIMEOUT` | `600` |
| `grader_timeout` | `LABCTL_GRADER_TIMEOUT` | `120` |
| `enabled_providers` | `LABCTL_ENABLED_PROVIDERS` | empty |
| `provider_paths` | `LABCTL_PROVIDER_PATHS` | empty |
| `definition_paths` | `LABCTL_DEFINITION_PATHS` | empty |
| `allow_definition_override` | `LABCTL_ALLOW_DEFINITION_OVERRIDE` | `false` |
| `log_level` | `LABCTL_LOG_LEVEL` | `warning` |

Environment lists use the platform path separator (`:` on Linux). Booleans
accept `true`, `false`, `1`, or `0`. Timeouts are positive integers.
`libvirt_storage_root` has no CLI flag and must be an absolute, normalized,
existing real directory. It and every component must not be a symlink. The root
must be owned by and writable/searchable by the invoking user, and it must not
contain or be contained by the private data, state, cache, or runtime roots.

The application reads `$XDG_CONFIG_HOME/labctl/config.toml`, defaulting to
`~/.config/labctl/config.toml`. Library callers may choose another path. Derived
application roots are:

| Purpose | Environment and fallback |
| --- | --- |
| Config | `$XDG_CONFIG_HOME/labctl`, else `~/.config/labctl` |
| Data | `$XDG_DATA_HOME/labctl`, else `~/.local/share/labctl` |
| Cache | `$XDG_CACHE_HOME/labctl`, else `~/.cache/labctl` |
| State | `$XDG_STATE_HOME/labctl`, else `~/.local/state/labctl` |
| Runtime | `$XDG_RUNTIME_DIR/labctl`, else `/tmp/labctl-$UID` |

Set XDG base-directory variables to absolute paths. labctl rejects a non-empty
relative `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`,
or `XDG_RUNTIME_DIR` before creating any directory; an empty value uses the
documented fallback.

Definitions default to `DATA/definitions/*/lab.yaml`. Definition snapshots,
SSH keys, `network.xml`, cloud-init source, and `known_hosts` remain private at
`DATA/instances/ID`. QEMU-visible copied bases, overlays, and seed ISOs use
`LIBVIRT_STORAGE_ROOT/INSTANCE_UID`; image blobs and references remain in the
private `CACHE/images` source cache. State uses `STATE/labs/ID.json`; trust uses
`STATE/trust/definitions.json`; and locks use `RUNTIME/locks`. Logs belong under
`STATE/logs`. New state records the exact storage path. A schema-v1 state without
`storage_path` retains the historical `DATA/instances/ID` layout and is never
silently migrated or redirected by current configuration.

## Guest proxy inheritance

At **creation**, labctl captures only `http_proxy`, `https_proxy`, `ftp_proxy`,
`all_proxy`, and `no_proxy`, plus their uppercase variants, from the invoking
process. For each pair, lowercase wins, including an explicitly empty value;
the selected value is installed in **both cases** for guest clients. No other
host environment is copied. With none of these variables set, cloud-init keeps
its original setup path and no proxy files are installed.

Proxy URLs are preserved literally, including credentials, quotes, dollar signs,
percent signs, backslashes and shell metacharacters; they are not evaluated or
rewritten. Control characters (including tabs, newlines and NUL), Unicode line
separators, and invalid Unicode surrogate characters are rejected with a generic
error that does not repeat the value. Export variables in your shell before
creating the lab; avoid placing credentials in commands recorded in shell history.

When at least one proxy URL is nonempty, labctl preserves the selected `no_proxy`
text and appends `localhost,127.0.0.1,::1`. This protects guest-local graders and
services even if `no_proxy` was explicitly empty. KVM creation also appends only
that lab's managed `10.200.N.0/24` network, VM names, and hostnames—not all private
networks. Existing entries are not normalized or removed; duplicate bypasses are
harmless. Both bypass cases receive the same result. CIDR bypass support varies
by client (notably older curl/urllib clients): add exact internal IP addresses to
`no_proxy` when using such clients. Merely listing a hostname does not create DNS
records. A `no_proxy` value without a nonempty proxy is copied without additions.

The guest setup wrapper installs the environment **before the setup script's
first package/network operation**. Non-login `runuser -u student -- env ...`
commands in the CT labs retain it for initial rootless Podman pulls. Persistence
uses labctl-owned files rather than replacing vendor environment files:

- `/etc/profile.d/labctl-proxy.sh` for login shells;
- an SSH `SetEnv` drop-in for interactive and noninteractive SSH sessions,
  including Ansible connections. The guest installer merges existing global
  `SetEnv` entries, checks the effective configuration, and reloads `sshd` before
  setup. This is the SSH-session equivalent of PAM `/etc/environment`: PAM's
  parser cannot faithfully preserve embedded `#` in arbitrary proxy values;
- `/etc/sudoers.d/90-labctl-proxy`, retaining only the ten proxy variable names
  through sudo for root package and Ansible become operations;
- a systemd user-environment generator for rootless Quadlet/user services.

These paths target the bundled Rocky 9 guest: Python 3, systemd user environment
generators, and OpenSSH with `sshd_config.d` inclusion are required. Custom
images or SSH `Match` rules may need adaptation; global SSH configuration that
masks the drop-in causes setup to fail rather than claiming successful inheritance.
Non-shell, non-SSH PAM consumers are not configured. Client-specific proxy
support still applies; labctl does not install corporate CA certificates.

A proxy bound only to the **host's loopback** is not reachable at that address
inside a VM. Use an externally reachable host address/listener (accessible from
the managed NAT network), with suitable firewall and access controls. labctl
never rebinds a host service, opens a firewall, or rewrites a proxy address.

**Lifecycle:** proxy values are captured in the creation seed, not JSON state.
Start/stop/reconcile do not mutate guest configuration. Full and selective resets
rebuild overlays using the **same seed**, so they restore the creation-time proxy
settings even if the invoking host environment changed. Resetting an instance
created before this feature does not add proxies. Remove and recreate the lab to
capture changed values; no existing VM is silently updated on upgrade.
