"""Application handlers that connect the CLI to safe, available facilities."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from labctl.config import Config, ConfigError, load_config
from labctl.definitions import LabDefinition, directory_digest, discover, load_definition
from labctl.errors import ExitStatus, LabctlError
from labctl.grading import GradeOutcome, run_grader
from labctl.images import ImageCatalog, ImageError, ImageStore
from labctl.kvm import KVMProvider
from labctl.orchestrator import KVMOrchestrator, OrchestrationError, OrchestrationNotFoundError
from labctl.paths import XDGPaths
from labctl.provider import Provider, discover_providers
from labctl.ssh import build_ssh_command
from labctl.state import StateError, load_state
from labctl.trust import TrustStore

ChildRunner = Callable[[list[str]], int]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CommandResult:
    kind: str
    data: Any
    columns: tuple[str, ...] = ()
    status: int = 0
    diagnostics: tuple[str, ...] = ()


def _run_child(argv: list[str]) -> int:
    return subprocess.run(argv, check=False).returncode  # noqa: S603


class Application:
    """Stateless command dispatcher over XDG-backed persistent data."""

    def __init__(
        self,
        *,
        data_root: Path | None = None,
        state_root: Path | None = None,
        config_root: Path | None = None,
        cache_root: Path | None = None,
        runtime_root: Path | None = None,
        child: ChildRunner = _run_child,
        confirm: Callable[[str], bool] | None = None,
    ) -> None:
        paths = XDGPaths.from_environment()
        self.data_root = data_root or paths.data
        self.state_root = state_root or paths.state
        self.config_root = config_root or paths.config
        self.cache_root = cache_root or (data_root if data_root is not None else paths.cache)
        self.runtime_root = runtime_root or (
            state_root if state_root is not None else paths.runtime
        )
        self.child = child
        self.confirm = confirm or self._terminal_confirm

    @staticmethod
    def _terminal_confirm(prompt: str) -> bool:
        if not sys.stdin.isatty():
            return False
        return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}

    def _orchestrator(self, config: Config) -> KVMOrchestrator:
        return KVMOrchestrator(
            self.data_root,
            self.state_root,
            storage_root=Path(config.libvirt_storage_root),
            address_timeout=config.address_timeout,
            ssh_timeout=config.ssh_timeout,
            cloud_init_timeout=config.cloud_init_timeout,
            shutdown_timeout=config.shutdown_timeout,
            lock_root=self.runtime_root / "locks",
        )

    def _libvirt_storage_root(self, config: Config) -> Path:
        root = Path(config.libvirt_storage_root)
        if not root.is_absolute() or root != Path(os.path.abspath(root)):
            raise ConfigError("libvirt storage root must be absolute")
        try:
            metadata = root.stat()
        except OSError as exc:
            raise ConfigError(
                f"libvirt storage root must be an existing directory: {root}"
            ) from exc
        if not root.is_dir():
            raise ConfigError(f"libvirt storage root must be an existing directory: {root}")
        current = Path(root.anchor)
        for component in root.parts[1:]:
            current /= component
            if current.is_symlink():
                raise ConfigError(f"libvirt storage root has a symlink component: {current}")
        if metadata.st_uid != os.getuid():
            raise ConfigError(f"libvirt storage root is not owned by the invoking user: {root}")
        if not os.access(root, os.W_OK | os.X_OK):
            raise ConfigError(f"libvirt storage root is not writable and searchable: {root}")
        canonical_root = root.resolve(strict=True)
        for private in (
            self.data_root,
            self.state_root,
            self.config_root,
            self.cache_root,
            self.runtime_root,
        ):
            private_absolute = Path(os.path.abspath(private)).resolve(strict=False)
            if (
                canonical_root == private_absolute
                or canonical_root.is_relative_to(private_absolute)
                or private_absolute.is_relative_to(canonical_root)
            ):
                raise ConfigError(f"libvirt storage root overlaps private labctl paths: {root}")
        return root

    def execute(self, args: argparse.Namespace) -> CommandResult:
        config = self._config(args)
        method = getattr(self, f"_{args.group}_{args.command}", None)
        if method is None:
            raise LabctlError(
                f"{args.group} {args.command} has no implemented handler", ExitStatus.OPERATION
            )
        try:
            result: CommandResult = method(args, config)
            return result
        except LabctlError:
            raise
        except KeyError as exc:
            raise LabctlError(f"not found: {exc.args[0]}", ExitStatus.NOT_FOUND) from exc
        except (ImageError, StateError) as exc:
            raise LabctlError(str(exc), ExitStatus.OPERATION) from exc
        except OrchestrationNotFoundError as exc:
            raise LabctlError(str(exc), ExitStatus.NOT_FOUND) from exc
        except OrchestrationError as exc:
            message = str(exc)
            status = (
                ExitStatus.CONFLICT
                if "already exists" in message or "locked" in message
                else ExitStatus.OPERATION
            )
            raise LabctlError(message, status) from exc

    def _config(self, args: argparse.Namespace) -> Config:
        cli = {
            "provider": args.provider,
            "libvirt_uri": args.uri,
            "allow_definition_override": args.allow_definition_override or None,
            "log_level": "debug" if args.debug else "info" if args.verbose else None,
        }
        return load_config(self.config_root / "config.toml", cli=cli)

    def _definitions(self, config: Config) -> dict[str, LabDefinition]:
        bundled_root = Path(__file__).parent / "data" / "labs"
        bundled = (
            sorted(path.parent for path in bundled_root.glob("*/lab.yaml"))
            if bundled_root.is_dir()
            else []
        )
        external = [
            self.data_root / "definitions",
            *(Path(item) for item in config.definition_paths),
        ]
        return discover(bundled, external, allow_override=config.allow_definition_override)

    @property
    def _labs_root(self) -> Path:
        return self.state_root / "labs"

    def _states(self) -> dict[str, dict[str, Any]]:
        states: dict[str, dict[str, Any]] = {}
        if not self._labs_root.exists():
            return states
        for path in sorted(self._labs_root.glob("*.json")):
            state = load_state(path)
            states[state["id"]] = state
        return states

    def _state(self, lab_id: str) -> dict[str, Any]:
        if not lab_id or Path(lab_id).name != lab_id:
            raise LabctlError("invalid lab name", ExitStatus.USAGE)
        path = self._labs_root / f"{lab_id}.json"
        if not path.is_file():
            raise LabctlError(f"lab not found: {lab_id}", ExitStatus.NOT_FOUND)
        return load_state(path)

    def _providers(self, config: Config) -> dict[str, Provider]:
        search = [self.data_root / "providers", *(Path(path) for path in config.provider_paths)]
        external = discover_providers(config.enabled_providers, search_paths=search)
        if "kvm" in external:
            raise LabctlError(
                "external provider duplicates built-in identity 'kvm'", ExitStatus.CONFLICT
            )
        return {
            "kvm": KVMProvider(
                uri=config.libvirt_uri,
                storage_path=Path(config.libvirt_storage_root),
            ),
            **external,
        }

    @contextmanager
    def _stable_external_definition(
        self, definition: LabDefinition, accepted_digest: str | None
    ) -> Iterator[LabDefinition]:
        if accepted_digest is None:
            yield definition
            return
        staging_root = self.data_root / ".definition-staging"
        staging_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(staging_root, 0o700)
        temporary = Path(tempfile.mkdtemp(prefix="create-", dir=staging_root))
        snapshot = temporary / "definition"
        try:
            shutil.copytree(definition.source.parent, snapshot, symlinks=True)
            if directory_digest(snapshot) != accepted_digest:
                raise LabctlError(
                    "external definition changed while its trusted snapshot was captured",
                    ExitStatus.PREREQUISITE,
                )
            stable = load_definition(snapshot / "lab.yaml", external=True)
            yield stable
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def _lab_ls(self, args: argparse.Namespace, config: Config) -> CommandResult:
        definitions = self._definitions(config)
        states = self._states()
        rows: list[dict[str, object]] = []
        for lab_id in sorted(definitions.keys() | states.keys()):
            state = states.get(lab_id)
            definition = definitions.get(lab_id)
            status = str(state.get("state", "unknown")) if state else "available"
            provider = str(
                state.get("provider", definition.provider if definition else config.provider)
                if state
                else definition.provider
                if definition
                else config.provider
            )
            if args.available and state is not None:
                continue
            if args.active and state is None:
                continue
            if args.provider and provider != args.provider:
                continue
            rows.append(
                {
                    "id": lab_id,
                    "title": (
                        definition.title
                        if definition
                        else str(state.get("title", ""))
                        if state
                        else ""
                    ),
                    "state": status,
                    "provider": provider,
                    "uri": state.get("provider_uri", "") if state else "",
                }
            )
        return CommandResult("labs", rows, ("id", "title", "state", "provider", "uri"))

    def _lab_inspect(self, args: argparse.Namespace, config: Config) -> CommandResult:
        state = self._states().get(args.lab)
        definition = self._definitions(config).get(args.lab)
        if state is None and definition is None:
            raise LabctlError(f"lab not found: {args.lab}", ExitStatus.NOT_FOUND)
        data: dict[str, object] = dict(state or {})
        if definition:
            data.update(
                title=definition.title,
                goal=definition.goal,
                instructions=definition.instructions,
                definition=str(definition.source),
            )
        return CommandResult("lab", data)

    def _lab_create(self, args: argparse.Namespace, config: Config) -> CommandResult:
        try:
            definition = self._definitions(config)[args.id]
        except KeyError as exc:
            raise LabctlError(f"definition not found: {args.id}", ExitStatus.NOT_FOUND) from exc
        selected_provider = config.provider
        if definition.provider != selected_provider:
            raise LabctlError(
                f"definition requires provider {definition.provider!r}, but {selected_provider!r} "
                "was selected",
                ExitStatus.PREREQUISITE,
            )
        try:
            provider = self._providers(config)[selected_provider]
        except KeyError as exc:
            raise LabctlError(
                f"provider not found: {selected_provider}", ExitStatus.NOT_FOUND
            ) from exc
        if selected_provider != "kvm":
            raise LabctlError(
                f"provider {selected_provider!r} was discovered, but its lifecycle "
                "is not implemented",
                ExitStatus.PREREQUISITE,
            )
        if not isinstance(provider, KVMProvider):
            raise LabctlError("built-in KVM provider is invalid", ExitStatus.OPERATION)
        self._libvirt_storage_root(config)
        accepted_digest: str | None = None
        if definition.external:
            digest = directory_digest(definition.source.parent)
            trust = TrustStore(self.state_root / "trust" / "definitions.json")
            accepted = trust.accepted(digest) or args.trust_external
            if not accepted:
                accepted = self.confirm(
                    f"Trust external definition {definition.id} digest {digest}?"
                )
            if not accepted:
                raise LabctlError(
                    "external definition is not trusted; use --trust-external "
                    "in non-interactive use",
                    ExitStatus.PREREQUISITE,
                )
            trust.accept(digest)
            accepted_digest = digest
        with self._stable_external_definition(definition, accepted_digest) as stable_definition:
            return self._create_kvm(stable_definition, args, config, provider)

    def _create_kvm(
        self,
        definition: LabDefinition,
        args: argparse.Namespace,
        config: Config,
        provider: KVMProvider,
    ) -> CommandResult:
        images = ImageStore(self.cache_root / "images")
        catalog = ImageCatalog.bundled()
        for vm in definition.vms:
            logger.debug("resolving image %s for VM %s", vm.image, vm.name)
            try:
                images.resolve(vm.image)
            except ImageError as exc:
                if vm.image not in catalog:
                    raise LabctlError(
                        f"image is not cached: {vm.image}", ExitStatus.PREREQUISITE
                    ) from exc
                try:
                    logger.debug("pulling image %s", vm.image)
                    images.pull(catalog[vm.image])
                except ImageError as exc:
                    raise LabctlError(str(exc), ExitStatus.PREREQUISITE) from exc
        logger.debug("checking KVM provider prerequisites")
        health = provider.doctor()
        if not health.healthy:
            failures = "; ".join(
                check.detail for check in health.checks if check.status.value == "fail"
            )
            raise LabctlError(f"provider prerequisites failed: {failures}", ExitStatus.PREREQUISITE)
        logger.debug("creating lab instance %s", definition.id)
        state = self._orchestrator(config).create(
            definition,
            provider.connection_uri,
            images,
            allow_untrusted=args.allow_untrusted_image,
        )
        logger.debug("lab instance %s is ready", definition.id)
        return CommandResult("lab", state)

    def _lab_start(self, args: argparse.Namespace, config: Config) -> CommandResult:
        return CommandResult("lab", self._orchestrator(config).start(args.lab))

    def _lab_stop(self, args: argparse.Namespace, config: Config) -> CommandResult:
        return CommandResult("lab", self._orchestrator(config).stop(args.lab, force=args.force))

    def _lab_restart(self, args: argparse.Namespace, config: Config) -> CommandResult:
        return CommandResult("lab", self._orchestrator(config).restart(args.lab, force=args.force))

    def _lab_reset(self, args: argparse.Namespace, config: Config) -> CommandResult:
        if not args.force and not self.confirm(
            f"Reset {args.lab} and discard all learner changes?"
        ):
            raise LabctlError(
                "reset requires confirmation or --force in non-interactive use", ExitStatus.USAGE
            )
        state = self._orchestrator(config).reset(args.lab, ImageStore(self.cache_root / "images"))
        return CommandResult("lab", state)

    def _lab_reconcile(self, args: argparse.Namespace, config: Config) -> CommandResult:
        plan = self._orchestrator(config).reconcile(args.lab, repair=args.repair)
        return CommandResult("reconciliation", plan, ("resource", "action", "reason"))

    def _lab_rm(self, args: argparse.Namespace, config: Config) -> CommandResult:
        self._orchestrator(config).remove(args.lab, force=args.force)
        return CommandResult("lab", {"id": args.lab, "removed": True})

    def _lab_grade(self, args: argparse.Namespace, config: Config) -> CommandResult:
        orchestrator = self._orchestrator(config)
        with orchestrator.grade_session(args.lab) as session:
            drift = session.reconcile(repair=False)
            if drift:
                details = "; ".join(f"{item['resource']}: {item['reason']}" for item in drift)
                raise LabctlError(
                    f"grading refused because resource drift was detected: {details}",
                    ExitStatus.PREREQUISITE,
                )
            snapshot = self._instance_path(args.lab) / "definition" / "lab.yaml"
            if not snapshot.is_file():
                raise LabctlError(
                    f"definition snapshot for lab {args.lab} is unavailable",
                    ExitStatus.PREREQUISITE,
                )
            definition = load_definition(snapshot)
            if definition.grading.reset_vms:
                session.reset(
                    ImageStore(self.cache_root / "images"),
                    vm_names=definition.grading.reset_vms,
                )
                state = session.start()
            else:
                state = self._state(args.lab)
            vms = self._vms(state)
            hosts: dict[str, dict[str, object]] = {}
            for name, vm in vms.items():
                required = (
                    "hostname",
                    "address",
                    "ssh_user",
                    "identity_file",
                    "known_hosts",
                    "state",
                )
                if not all(isinstance(vm.get(key), str) and vm[key] for key in required):
                    raise LabctlError(
                        f"VM {name} lacks grading connection data", ExitStatus.PREREQUISITE
                    )
                hosts[name] = {
                    "name": name,
                    "hostname": vm["hostname"],
                    "address": vm["address"],
                    "ssh_user": vm["ssh_user"],
                    "private_key_path": vm["identity_file"],
                    "known_hosts_path": vm["known_hosts"],
                    "provider": state.get("provider", "kvm"),
                    "provider_uri": state["provider_uri"],
                    "lifecycle_state": vm["state"],
                }
            grade = run_grader(
                [str(definition.grader)],
                {
                    "lab_id": args.lab,
                    "title": definition.title,
                    "goal": definition.goal,
                    "provider": state.get("provider", "kvm"),
                    "provider_uri": state["provider_uri"],
                    "hosts": hosts,
                },
                timeout=config.grader_timeout,
            )
            status = 0 if grade.outcome is GradeOutcome.PASS else ExitStatus.GRADING
            return CommandResult(
                "grade",
                {
                    "outcome": grade.outcome.value,
                    "exit_code": grade.exit_code,
                    "stdout": list(grade.stdout_lines),
                    "stderr": list(grade.stderr_lines),
                    "timed_out": grade.timed_out,
                    "output_overflow": grade.output_overflow,
                },
                status=status,
                diagnostics=grade.stderr_lines,
            )

    @staticmethod
    def _target(target: str) -> tuple[str, str]:
        parts = target.split("/")
        if len(parts) != 2 or not all(parts) or any(Path(part).name != part for part in parts):
            raise LabctlError("VM target must be LAB/VM", ExitStatus.USAGE)
        return parts[0], parts[1]

    def _vms(self, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
        vms = state.get("vms", {})
        if not isinstance(vms, dict) or not all(
            isinstance(name, str) and isinstance(value, dict) for name, value in vms.items()
        ):
            raise LabctlError("lab state has invalid VM data", ExitStatus.OPERATION)
        return vms

    def _vm_ls(self, args: argparse.Namespace, _config: Config) -> CommandResult:
        states = {args.lab_id: self._state(args.lab_id)} if args.lab_id else self._states()
        rows: list[dict[str, object]] = []
        for lab_id, state in states.items():
            for name, vm in sorted(self._vms(state).items()):
                status = str(vm.get("state", "unknown"))
                rows.append({"lab": lab_id, "name": name, "state": status})
        return CommandResult("vms", rows, ("lab", "name", "state"))

    def _vm_inspect(self, args: argparse.Namespace, _config: Config) -> CommandResult:
        lab_id, vm_name = args.lab_id, args.vm_name
        try:
            vm = self._vms(self._state(lab_id))[vm_name]
        except KeyError as exc:
            raise LabctlError(f"VM not found: {lab_id}/{vm_name}", ExitStatus.NOT_FOUND) from exc
        return CommandResult("vm", {"lab": lab_id, "name": vm_name, **vm})

    def _connection(self, lab_id: str, vm_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        state = self._state(lab_id)
        try:
            vm = self._vms(state)[vm_name]
        except KeyError as exc:
            raise LabctlError(f"VM not found: {lab_id}/{vm_name}", ExitStatus.NOT_FOUND) from exc
        return state, vm

    @staticmethod
    def _default_vm(state: dict[str, Any]) -> str:
        vms = state.get("vms")
        if not isinstance(vms, dict) or len(vms) != 1:
            raise LabctlError(
                "specify a VM when the lab does not contain exactly one", ExitStatus.USAGE
            )
        return str(next(iter(vms)))

    def _ssh(self, lab_id: str, vm_name: str, remote: tuple[str, ...]) -> CommandResult:
        _state, vm = self._connection(lab_id, vm_name)
        required = ("ssh_user", "address", "identity_file", "known_hosts")
        if not all(isinstance(vm.get(key), str) and vm[key] for key in required):
            raise LabctlError("VM state lacks SSH connection data", ExitStatus.PREREQUISITE)
        argv = build_ssh_command(
            vm["ssh_user"],
            vm["address"],
            Path(vm["identity_file"]),
            Path(vm["known_hosts"]),
            remote,
        )
        return CommandResult("child", None, status=self.child(argv))

    def _vm_ssh(self, args: argparse.Namespace, _config: Config) -> CommandResult:
        return self._ssh(args.lab_id, args.vm_name, ())

    def _lab_ssh(self, args: argparse.Namespace, _config: Config) -> CommandResult:
        state = self._state(args.lab)
        vm_name = args.vm or self._default_vm(state)
        return self._ssh(args.lab, vm_name, ())

    def _console(self, lab_id: str, vm_name: str, uri_override: str | None) -> CommandResult:
        state, vm = self._connection(lab_id, vm_name)
        domain = vm.get("domain")
        uri = state.get("provider_uri")
        if uri_override is not None and uri_override != uri:
            raise LabctlError(
                f"instance is permanently bound to {uri}; refusing URI override {uri_override}",
                ExitStatus.CONFLICT,
            )
        if not isinstance(domain, str) or not domain or not isinstance(uri, str) or not uri:
            raise LabctlError("VM state lacks console connection data", ExitStatus.PREREQUISITE)
        argv = ["virsh", "--connect", uri, "console", domain]
        return CommandResult("child", None, status=self.child(argv))

    def _vm_console(self, args: argparse.Namespace, _config: Config) -> CommandResult:
        return self._console(args.lab_id, args.vm_name, args.uri)

    def _lab_console(self, args: argparse.Namespace, _config: Config) -> CommandResult:
        state = self._state(args.lab)
        return self._console(args.lab, args.vm or self._default_vm(state), args.uri)

    def _catalog_rows(self) -> list[dict[str, object]]:
        catalog = ImageCatalog.bundled()
        references = ImageStore(self.cache_root / "images").references()
        rows = [
            {
                "id": image.id,
                "format": image.format,
                "available": image.available,
                "cached": image.id in references,
                "sha256": references.get(image.id, image.sha256 or ""),
                "reason": image.unavailable_reason or "",
            }
            for image in sorted(catalog.values(), key=lambda item: item.id)
        ]
        rows.extend(
            {
                "id": image_id,
                "format": "qcow2",
                "available": False,
                "cached": True,
                "sha256": digest,
                "reason": "locally imported image",
            }
            for image_id, digest in references.items()
            if image_id not in catalog
        )
        return sorted(rows, key=lambda row: str(row["id"]))

    def _image_ls(self, args: argparse.Namespace, _config: Config) -> CommandResult:
        rows = self._catalog_rows()
        if args.cached:
            rows = [row for row in rows if row["cached"]]
        if args.available:
            rows = [row for row in rows if row["available"]]
        return CommandResult("images", rows, ("id", "format", "available", "cached"))

    def _image_inspect(self, args: argparse.Namespace, _config: Config) -> CommandResult:
        rows = {str(row["id"]): row for row in self._catalog_rows()}
        references = ImageStore(self.cache_root / "images").references()
        if args.image not in rows and args.image not in references:
            raise LabctlError(f"image not found: {args.image}", ExitStatus.NOT_FOUND)
        row = rows.get(args.image)
        if row is None:
            row = {"id": args.image, "cached": True, "sha256": references[args.image]}
        if args.verbose and not args.json:
            row["path"] = (
                str(ImageStore(self.cache_root / "images").resolve(args.image)[0])
                if row["cached"]
                else "not cached"
            )
        return CommandResult("image", row)

    def _image_pull(self, args: argparse.Namespace, _config: Config) -> CommandResult:
        catalog = ImageCatalog.bundled()
        try:
            image = catalog[args.image]
        except KeyError as exc:
            raise LabctlError(f"image not found: {args.image}", ExitStatus.NOT_FOUND) from exc
        path = ImageStore(
            self.cache_root / "images", progress=getattr(args, "download_progress", None)
        ).pull(image)
        return CommandResult("image", {"id": args.image, "path": str(path)})

    def _image_import(self, args: argparse.Namespace, _config: Config) -> CommandResult:
        path = ImageStore(self.cache_root / "images").import_qcow2(
            args.image, Path(args.source), trusted_sha256=args.checksum
        )
        return CommandResult("image", {"id": args.image, "path": str(path)})

    def _image_rm(self, args: argparse.Namespace, _config: Config) -> CommandResult:
        referenced = any(
            vm.get("image") == args.image
            for state in self._states().values()
            for vm in self._vms(state).values()
        )
        if referenced and not args.force:
            raise LabctlError(
                f"image {args.image} is referenced by an instance; use --force "
                "to remove its logical entry",
                ExitStatus.CONFLICT,
            )
        ImageStore(self.cache_root / "images").delete(args.image, force=True)
        return CommandResult("image", {"id": args.image, "removed": True})

    def _vm_start(self, args: argparse.Namespace, config: Config) -> CommandResult:
        return CommandResult(
            "vm", self._orchestrator(config).start(args.lab_id, vm_name=args.vm_name)
        )

    def _vm_stop(self, args: argparse.Namespace, config: Config) -> CommandResult:
        return CommandResult(
            "vm",
            self._orchestrator(config).stop_vm(args.lab_id, args.vm_name, force=args.force),
        )

    def _vm_restart(self, args: argparse.Namespace, config: Config) -> CommandResult:
        return CommandResult(
            "vm",
            self._orchestrator(config).restart(args.lab_id, vm_name=args.vm_name, force=args.force),
        )

    def _vm_rm(self, args: argparse.Namespace, config: Config) -> CommandResult:
        return CommandResult(
            "vm",
            self._orchestrator(config).remove_vm(args.lab_id, args.vm_name, force=args.force),
        )

    def _instance_path(self, lab_id: str) -> Path:
        return self.data_root / "instances" / lab_id

    def _provider_ls(self, _args: argparse.Namespace, config: Config) -> CommandResult:
        rows = [
            {"id": provider.id, "api_version": provider.api_version}
            for provider in self._providers(config).values()
        ]
        return CommandResult("providers", rows, ("id", "api_version"))

    def _provider_inspect(self, args: argparse.Namespace, config: Config) -> CommandResult:
        try:
            provider = self._providers(config)[args.provider_id]
        except KeyError as exc:
            raise LabctlError(
                f"provider not found: {args.provider_id}", ExitStatus.NOT_FOUND
            ) from exc
        capabilities = provider.capabilities()
        return CommandResult(
            "provider",
            {
                "id": provider.id,
                "api_version": provider.api_version,
                "compute": capabilities.compute,
                "storage": capabilities.storage,
                "network": capabilities.network,
            },
        )

    def _provider_doctor(self, args: argparse.Namespace, config: Config) -> CommandResult:
        provider_id = args.provider_id or config.provider
        try:
            provider = self._providers(config)[provider_id]
        except KeyError as exc:
            raise LabctlError(f"provider not found: {provider_id}", ExitStatus.NOT_FOUND) from exc
        health = provider.doctor()
        rows = [
            {
                "name": check.name,
                "status": check.status.value,
                "detail": check.detail,
                "guidance": check.guidance or "",
            }
            for check in health.checks
        ]
        status = 0 if health.healthy else ExitStatus.PREREQUISITE
        return CommandResult("doctor", rows, ("name", "status", "detail", "guidance"), status)
