"""Concrete transactional KVM/libvirt orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import shutil
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from functools import partial
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import yaml
from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException

from labctl import controller_access
from labctl.definitions import (
    LAB_ID,
    VM_NAME,
    ControllerAccessDefinition,
    LabDefinition,
    VMDefinition,
    directory_digest,
    load_definition,
    parse_size,
)
from labctl.images import ImageError, ImageStore
from labctl.kvm import KVMProvider
from labctl.locks import LockConflict, file_lock
from labctl.proxy import configure_proxy
from labctl.ssh import build_ssh_command, generate_keypair, write_known_host
from labctl.state import StateError, load_state, save_json, save_state, validate_state
from labctl.subprocesses import CommandRunner, Runner

ReadinessProbe = Callable[[str, str], bool]
KeyGenerator = Callable[[Path], tuple[Path, Path]]
WaitStopped = Callable[[str, float], bool]
_METADATA_URI = "urn:labctl"
_METADATA_TAG = f"{{{_METADATA_URI}}}ownership"
logger = logging.getLogger(__name__)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


class OrchestrationError(RuntimeError):
    """A resource operation failed, with rollback details where applicable."""


class OrchestrationNotFoundError(OrchestrationError):
    """A requested recorded lab or VM does not exist."""


class GradeSession:
    """Lock-bound lifecycle operations used to prepare and run one grader."""

    def __init__(self, orchestrator: KVMOrchestrator, lab_id: str) -> None:
        self._orchestrator = orchestrator
        self._lab_id = lab_id

    def reconcile(self, *, repair: bool) -> list[dict[str, str]]:
        return self._orchestrator._reconcile_locked(self._lab_id, repair=repair)

    def reset(self, images: ImageStore, *, vm_names: tuple[str, ...]) -> dict[str, Any]:
        return self._orchestrator._reset_locked(self._lab_id, images, vm_names=vm_names)

    def start(self) -> dict[str, Any]:
        return self._orchestrator._start_locked(self._lab_id)


def _host_keypair(directory: Path) -> tuple[Path, Path]:
    return generate_keypair(directory, name="ssh_host_ed25519_key")


class KVMOrchestrator:
    """Own isolated libvirt resources and explicit per-user state."""

    def __init__(
        self,
        data_root: Path,
        state_root: Path,
        *,
        runner: CommandRunner | None = None,
        keygen: KeyGenerator = generate_keypair,
        host_keygen: KeyGenerator = _host_keypair,
        address_timeout: float = 120,
        ssh_timeout: float = 180,
        cloud_init_timeout: float = 600,
        shutdown_timeout: float = 60,
        lock_root: Path | None = None,
        storage_root: Path | None = None,
    ) -> None:
        self.data_root = data_root
        self.state_root = state_root
        self.runner = runner or Runner()
        self.keygen = keygen
        self.host_keygen = host_keygen
        self.address_timeout = address_timeout
        self.ssh_timeout = ssh_timeout
        self.cloud_init_timeout = cloud_init_timeout
        self.shutdown_timeout = shutdown_timeout
        self.lock_root = lock_root or state_root / "locks"
        self.storage_root = storage_root

    @property
    def labs_state(self) -> Path:
        return self.state_root / "labs"

    @staticmethod
    def _validate_lab_id(lab_id: str) -> None:
        if not isinstance(lab_id, str) or LAB_ID.fullmatch(lab_id) is None:
            raise OrchestrationError(f"invalid lab ID: {lab_id!r}")

    @staticmethod
    def _validate_vm_name(vm_name: str) -> None:
        if not isinstance(vm_name, str) or VM_NAME.fullmatch(vm_name) is None:
            raise OrchestrationError(f"invalid VM name: {vm_name!r}")

    def _state_path(self, lab_id: str) -> Path:
        return self.labs_state / f"{lab_id}.json"

    def _instance_path(self, lab_id: str) -> Path:
        return self.data_root / "instances" / lab_id

    def _storage_instance(self, state: dict[str, Any], *, marker: bool = True) -> Path:
        lab_id = str(state.get("id"))
        persisted = state.get("storage_path")
        if persisted is None:
            return self._instance_path(lab_id)
        uid = self._uid(state)
        if self.storage_root is None:
            raise OrchestrationError("configured storage root changed for existing lab")
        try:
            root = Path(os.path.abspath(self.storage_root)).resolve(strict=True)
        except OSError as exc:
            raise OrchestrationError("configured storage root changed for existing lab") from exc
        expected = root / uid
        persisted_path = Path(persisted) if isinstance(persisted, str) else None
        if (
            persisted_path is None
            or not persisted_path.is_absolute()
            or persisted_path != expected
            or expected.resolve(strict=False).parent != root
        ):
            raise OrchestrationError("configured storage root changed for existing lab")
        if marker:
            self._validate_storage_marker(expected, lab_id, uid)
        return expected

    @staticmethod
    def _write_storage_marker(path: Path, lab_id: str, uid: str) -> None:
        marker = {
            "schema_version": 1,
            "provider": "kvm",
            "lab_id": lab_id,
            "instance_uid": uid,
            "resource_type": "storage",
        }
        descriptor, temporary = tempfile.mkstemp(prefix=".owner.", dir=path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(marker, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path / ".labctl-owner.json")
            KVMOrchestrator._fsync_directory(path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    def _validate_storage_marker(self, path: Path, lab_id: str, uid: str) -> None:
        expected = {
            "schema_version": 1,
            "provider": "kvm",
            "lab_id": lab_id,
            "instance_uid": uid,
            "resource_type": "storage",
        }
        try:
            with self._open_directory_no_symlinks(path) as directory:
                directory_metadata = os.fstat(directory)
                if directory_metadata.st_uid != os.getuid():
                    raise OrchestrationError(f"invalid storage ownership marker: {path}")
                metadata = os.stat(".labctl-owner.json", dir_fd=directory, follow_symlinks=False)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_size > 4096
                ):
                    raise OrchestrationError(f"invalid storage ownership marker: {path}")
                descriptor = os.open(
                    ".labctl-owner.json",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory,
                )
                try:
                    raw = os.read(descriptor, 4097)
                finally:
                    os.close(descriptor)
        except (FileNotFoundError, OSError) as exc:
            raise OrchestrationError(f"invalid storage ownership marker: {path}") from exc
        try:
            observed = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise OrchestrationError(f"invalid storage ownership marker: {path}") from exc
        if observed != expected:
            raise OrchestrationError(f"invalid storage ownership marker: {path}")

    @staticmethod
    def _copy_verified_base(source: Path, digest: str, storage: Path) -> Path:
        destination = storage / "bases" / digest
        if destination.exists():
            if destination.is_symlink() or not destination.is_file():
                raise OrchestrationError(f"copied image blob is not regular: {digest}")
            actual = hashlib.sha256()
            with destination.open("rb") as copied:
                for chunk in iter(lambda: copied.read(1024 * 1024), b""):
                    actual.update(chunk)
            if actual.hexdigest() != digest:
                raise OrchestrationError(f"copied image blob checksum mismatch: {digest}")
            return destination
        KVMOrchestrator._mkdir_shared(destination.parent, exist_ok=True)
        temporary = destination.with_name(f".{digest}.tmp")
        shutil.copyfile(source, temporary)
        actual = hashlib.sha256()
        with temporary.open("rb") as copied:
            for chunk in iter(lambda: copied.read(1024 * 1024), b""):
                actual.update(chunk)
        if actual.hexdigest() != digest:
            temporary.unlink(missing_ok=True)
            raise OrchestrationError(f"copied image blob checksum mismatch: {digest}")
        temporary.chmod(0o444)
        with temporary.open("rb") as copied:
            os.fsync(copied.fileno())
        os.replace(temporary, destination)
        KVMOrchestrator._fsync_directory(destination.parent)
        return destination

    @staticmethod
    def _mkdir_shared(path: Path, *, exist_ok: bool = False) -> None:
        """Create a QEMU-visible directory without a private process umask."""
        previous_umask = os.umask(0)
        try:
            path.mkdir(mode=0o2750, exist_ok=exist_ok)
        finally:
            os.umask(previous_umask)
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o2750
            or metadata.st_gid != path.parent.stat().st_gid
        ):
            raise OrchestrationError(f"shared storage directory is not safely provisioned: {path}")
        KVMOrchestrator._fsync_directory(path)
        KVMOrchestrator._fsync_directory(path.parent)

    def _journal_path(self, lab_id: str) -> Path:
        return self.state_root / "transactions" / f"{lab_id}.json"

    @staticmethod
    def _unlink_durable(path: Path) -> None:
        path.unlink(missing_ok=True)
        if path.parent.exists():
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with KVMOrchestrator._open_directory_no_symlinks(path.parent) as parent:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(parent)

    def _fsync_storage_hierarchy(self, instance: Path) -> None:
        root = (
            self.storage_root
            if self.storage_root is not None
            and (instance == self.storage_root or instance.is_relative_to(self.storage_root))
            else self.data_root
        )
        for directory in dict.fromkeys((instance.parent, root)):
            if directory.exists():
                self._fsync_directory(directory)

    def _fsync_tree(self, root: Path) -> None:
        directories = [root]
        for path in root.rglob("*"):
            if path.is_symlink():
                continue
            if path.is_dir():
                directories.append(path)
                continue
            if not path.is_file():
                continue
            with self._open_directory_no_symlinks(path.parent) as parent:
                descriptor = os.open(
                    path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
                )
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
            self._fsync_directory(directory)
        self._fsync_storage_hierarchy(root)

    def _rmtree_durable(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=False)
        self._fsync_storage_hierarchy(path)

    def _load_journal(self, lab_id: str) -> dict[str, Any] | None:
        path = self._journal_path(lab_id)
        if not path.exists():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OrchestrationError(f"transaction journal is unreadable: {path}") from exc
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != 1
            or document.get("id") != lab_id
        ):
            raise OrchestrationError(f"transaction journal is invalid: {path}")
        return document

    def _pending_transaction_error(
        self, lab_id: str, journal: dict[str, Any], operation: str
    ) -> OrchestrationError:
        pending = journal.get("operation")
        if pending == "create":
            recovery = (
                f"labctl lab rm {lab_id} (discard incomplete creation) or "
                f"labctl lab create {lab_id} (retry creation)"
            )
        elif pending == "reset":
            recovery = f"labctl lab reset {lab_id}"
        elif pending == "remove":
            recovery = f"labctl lab rm {lab_id}"
        elif pending == "remove-vm":
            vm_name = journal.get("vm_name")
            if isinstance(vm_name, str) and VM_NAME.fullmatch(vm_name):
                recovery = f"labctl vm rm {lab_id} {vm_name} --force"
            else:
                recovery = "inspect the transaction journal; invalid VM name prevents safe recovery"
        else:
            recovery = "inspect the transaction journal; no automatic recovery command is known"
        return OrchestrationError(
            f"pending transaction ({pending}) for {lab_id} requires recovery before {operation}: "
            f"{recovery}"
        )

    def _require_no_pending_transaction(self, lab_id: str, operation: str) -> None:
        journal = self._load_journal(lab_id)
        if journal is not None:
            raise self._pending_transaction_error(lab_id, journal, operation)

    def _validated_create_journal(
        self, journal: dict[str, Any]
    ) -> tuple[str, str, str, str, list[str], dict[str, dict[str, Any]], bool]:
        lab_id = journal.get("id")
        uri = journal.get("provider_uri")
        uid = journal.get("instance_uid")
        network = journal.get("network")
        order = journal.get("vm_order")
        records = journal.get("vms")
        if not isinstance(lab_id, str):
            raise OrchestrationError("invalid create transaction: lab ID")
        try:
            self._validate_lab_id(lab_id)
        except OrchestrationError as exc:
            raise OrchestrationError("invalid create transaction: lab ID") from exc
        legacy_uid = f"u{os.getuid()}-{lab_id.lower()}"
        try:
            parsed_uid = uuid.UUID(uid) if isinstance(uid, str) else None
        except ValueError:
            parsed_uid = None
        if (
            journal.get("schema_version") != 1
            or journal.get("operation") != "create"
            or not isinstance(uri, str)
            or not uri
            or not isinstance(uid, str)
            or (uid != legacy_uid and (parsed_uid is None or str(parsed_uid) != uid))
            or network != KVMProvider.resource_name(uid, "network")
            or journal.get("instance_owned") is not True
            or not isinstance(order, list)
            or not all(isinstance(name, str) for name in order)
            or len(set(order)) != len(order)
            or not isinstance(records, dict)
            or not set(records).issubset(order)
        ):
            raise OrchestrationError("invalid create transaction: identity or VM records")
        for name in order:
            try:
                self._validate_vm_name(name)
            except OrchestrationError as exc:
                raise OrchestrationError("invalid create transaction: VM name") from exc

        state_path = self._state_path(lab_id)
        state: dict[str, Any] | None = None
        if state_path.exists():
            try:
                state = load_state(state_path)
            except StateError as exc:
                raise OrchestrationError(f"invalid create transaction: state: {exc}") from exc
            if (
                state.get("id") != lab_id
                or state.get("provider") != "kvm"
                or state.get("provider_uri") != uri
                or state.get("instance_uid") != uid
                or state.get("network") != network
                or state.get("vm_order") != order
                or not isinstance(state.get("vms"), dict)
                or not set(state["vms"]).issubset(order)
                or state.get("state") not in {"provisioning", "failed_cleanup", "ready"}
                or state.get("storage_path") != journal.get("storage_path")
                or (
                    state.get("storage_path") is not None
                    and journal.get("storage_owned") is not True
                )
            ):
                raise OrchestrationError("invalid create transaction: state identity mismatch")
            storage = self._storage_instance(state, marker=False)
            instance = self._instance_path(lab_id)
            if storage != instance and storage.exists():
                self._validate_storage_marker(storage, lab_id, uid)

        elif records:
            raise OrchestrationError("invalid create transaction: VM records lack state")

        validated: dict[str, dict[str, Any]] = {}
        for name, record in records.items():
            if not isinstance(record, dict) or state is None:
                raise OrchestrationError("invalid create transaction: VM record")
            vm = state["vms"].get(name)
            if not isinstance(vm, dict):
                raise OrchestrationError("invalid create transaction: VM state")
            domain = KVMProvider.resource_name(uid, name)
            try:
                argv = self._validated_recreation_argv(lab_id, name, state, vm)
            except OrchestrationError as exc:
                raise OrchestrationError(f"invalid create transaction: {exc}") from exc
            material = [
                domain,
                int(argv[6]) * 1024**2,
                int(argv[8]),
                [["disk", str(vm["overlay"])], ["cdrom", str(vm["seed"])]],
                "network",
                network,
                "virtio",
            ]
            if (
                record.get("domain") != domain
                or record.get("stage") not in {"virt-install-planned", "metadata-attached"}
                or record.get("material") != material
            ):
                raise OrchestrationError("invalid create transaction: VM material")
            validated[name] = {"domain": domain, "stage": record["stage"], "material": material}
        if (
            state is not None
            and state.get("state") == "ready"
            and (
                set(validated) != set(order)
                or any(record["stage"] != "metadata-attached" for record in validated.values())
                or any(vm.get("state") != "ready" for vm in state["vms"].values())
            )
        ):
            raise OrchestrationError("invalid create transaction: committed state mismatch")
        return lab_id, uri, uid, network, order, validated, state is not None

    def _recover_create(self, journal: dict[str, Any]) -> None:
        lab_id, uri, uid, network, order, vms, state_bound = self._validated_create_journal(journal)
        if not state_bound:
            self._unlink_durable(self._journal_path(lab_id))
            return
        if load_state(self._state_path(lab_id)).get("state") == "ready":
            self._unlink_durable(self._journal_path(lab_id))
            return
        failures: list[str] = []
        for name in reversed(order):
            record = vms.get(name)
            if record is None:
                continue
            domain = record["domain"]
            try:
                if self._resource_exists(uri, "dominfo", domain, "domain"):
                    try:
                        self._verify_domain(uri, domain, uid, str(name))
                    except OrchestrationError:
                        expected = record.get("material")
                        observed = self._domain_material(
                            self._virsh(uri, "dumpxml", domain, check=False)
                        )
                        normalized = json.loads(json.dumps(observed))
                        if record.get("stage") != "virt-install-planned" or normalized != expected:
                            raise
                        self._virsh(
                            uri,
                            "metadata",
                            domain,
                            "--config",
                            "--live",
                            "--key",
                            "labctl",
                            "--uri",
                            "urn:labctl",
                            "--set",
                            self._ownership_xml(uid, "domain", str(name)),
                        )
                self._cleanup_created_domain(uri, domain, uid, str(name))
            except Exception as exc:
                failures.append(f"{domain}: {exc}")
        try:
            if self._resource_exists(uri, "net-info", network, "network"):
                self._verify_network(uri, network, uid)
                if self._network_active(uri, network):
                    self._virsh(uri, "net-destroy", network)
                if self._resource_exists(uri, "net-info", network, "network"):
                    self._verify_network(uri, network, uid)
                    self._virsh(uri, "net-undefine", network)
            if self._resource_exists(uri, "net-info", network, "network"):
                raise OrchestrationError(f"network remains after recovery cleanup: {network}")
        except Exception as exc:
            failures.append(f"{network}: {exc}")
        if failures:
            raise OrchestrationError("create recovery failed: " + "; ".join(failures))
        state = load_state(self._state_path(lab_id))
        instance = self._instance_path(lab_id)
        storage = self._storage_instance(state, marker=False)
        if storage != instance and storage.exists():
            self._validate_storage_marker(storage, lab_id, uid)
            self._rmtree_durable(storage)
        if state_bound and instance.exists():
            self._rmtree_durable(instance)
        self._unlink_durable(self._state_path(lab_id))
        self._unlink_durable(self._journal_path(lab_id))

    def _log_failure(self, lab_id: str, message: str) -> None:
        logs = self.state_root / "logs"
        logs.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(logs, 0o700)
        path = logs / f"{lab_id}.log"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as output:
            output.write(f"{message}\n")

    def _virsh(self, uri: str, *arguments: str, check: bool = True) -> str:
        return self.runner.run(["virsh", "--connect", uri, *arguments], check=check).stdout

    def _resource_exists(self, uri: str, probe: str, resource: str, kind: str) -> bool:
        result = self.runner.run(["virsh", "--connect", uri, probe, resource], check=False)
        if result.returncode == 0:
            return True
        detail = (result.stderr.strip() or result.stdout.strip()).lower()
        expected = resource.lower()
        if f"failed to get {kind} '{expected}'" in detail or (
            f"{kind} not found" in detail and expected in detail
        ):
            return False
        raise OrchestrationError(
            f"{kind} probe failed for {resource}: "
            f"{result.stderr.strip() or result.stdout.strip() or 'no diagnostic output'}"
        )

    def _cleanup_created_domain(self, uri: str, domain: str, uid: str, vm_name: str) -> None:
        if not self._resource_exists(uri, "dominfo", domain, "domain"):
            return
        self._verify_domain(uri, domain, uid, vm_name)
        if self._domain_active(uri, domain):
            self._verify_domain(uri, domain, uid, vm_name)
            self._virsh(uri, "destroy", domain)
        self._verify_domain(uri, domain, uid, vm_name)
        self._virsh(uri, "undefine", domain, "--nvram")

    @contextmanager
    def _lab_lock(self, lab_id: str) -> Iterator[None]:
        self._validate_lab_id(lab_id)
        try:
            with file_lock(self.lock_root / f"lab-{lab_id}.lock"):
                yield
        except LockConflict as exc:
            raise OrchestrationError(str(exc)) from exc

    @contextmanager
    def grade_session(self, lab_id: str) -> Iterator[GradeSession]:
        """Hold the per-lab lifecycle lock until grading has fully completed."""
        with self._lab_lock(lab_id):
            journal = self._load_journal(lab_id)
            if journal is not None:
                if journal.get("operation") != "reset":
                    raise self._pending_transaction_error(lab_id, journal, "grading")
                self._recover_reset(journal)
                if self._load_journal(lab_id) is not None:
                    raise self._pending_transaction_error(lab_id, journal, "grading")
            yield GradeSession(self, lab_id)

    @staticmethod
    def _ownership(uid: str, resource_type: str, vm_name: str | None = None) -> dict[str, str]:
        ownership = {
            "provider": "kvm",
            "uid": uid,
            "resource-type": resource_type,
        }
        if resource_type == "domain":
            if vm_name is None:
                raise ValueError("domain ownership requires a VM name")
            ownership["vm-name"] = vm_name
        return ownership

    @classmethod
    def _ownership_xml(cls, uid: str, resource_type: str, vm_name: str | None = None) -> str:
        owner = ElementTree.Element(_METADATA_TAG)
        for key, value in cls._ownership(uid, resource_type, vm_name).items():
            ElementTree.SubElement(owner, f"{{{_METADATA_URI}}}{key}").text = value
        return ElementTree.tostring(owner, encoding="unicode")

    @staticmethod
    def _parse_ownership(xml: str, *, network: bool = False) -> dict[str, str] | None:
        try:
            root = SafeElementTree.fromstring(xml)
        except (ElementTree.ParseError, DefusedXmlException):
            return None
        owner = root.find(f"metadata/{_METADATA_TAG}") if network else root
        if owner is None:
            return None
        if owner.tag == _METADATA_TAG:
            prefix = f"{{{_METADATA_URI}}}"
        elif not network and owner.tag == "ownership":
            # `virsh metadata --uri` may omit namespace qualification from
            # URI-scoped metadata when serializing it for retrieval.
            prefix = ""
        else:
            return None
        fields: dict[str, str] = {}
        for child in owner:
            if prefix:
                if not child.tag.startswith(prefix):
                    return None
                key = child.tag[len(prefix) :]
            else:
                if not isinstance(child.tag, str) or child.tag.startswith("{"):
                    return None
                key = child.tag
            if key in fields:
                return None
            fields[key] = child.text or ""
        return fields

    @staticmethod
    def _uid(state: dict[str, Any]) -> str:
        uid = state.get("instance_uid")
        if not isinstance(uid, str) or not uid:
            raise OrchestrationError("lab state lacks persistent ownership identity")
        lab_id = state.get("id")
        legacy = False
        if isinstance(lab_id, str) and lab_id:
            prefix, suffix = "u", f"-{lab_id.lower()}"
            numeric_uid = uid[len(prefix) : -len(suffix)] if uid.endswith(suffix) else ""
            legacy = uid.startswith(prefix) and numeric_uid.isdecimal()
        try:
            parsed = uuid.UUID(uid)
        except ValueError:
            parsed = None
        if not legacy and (parsed is None or str(parsed) != uid):
            raise OrchestrationError("lab state has invalid persistent ownership identity")
        return uid

    def _network_xml(self, name: str, uid: str) -> str:
        network = ElementTree.Element("network")
        ElementTree.SubElement(network, "name").text = name
        metadata = ElementTree.SubElement(network, "metadata")
        owner = ElementTree.SubElement(metadata, "{urn:labctl}ownership")
        for key, value in self._ownership(uid, "network").items():
            ElementTree.SubElement(owner, f"{{{_METADATA_URI}}}{key}").text = value
        ElementTree.SubElement(network, "forward", {"mode": "nat"})
        identity = uuid.uuid5(uuid.NAMESPACE_OID, uid)
        bridge_suffix = identity.hex[:8]
        subnet = 16 + identity.int % 224
        ElementTree.SubElement(network, "bridge", {"name": f"lc{bridge_suffix}"})
        ip = ElementTree.SubElement(
            network, "ip", {"address": f"10.200.{subnet}.1", "netmask": "255.255.255.0"}
        )
        dhcp = ElementTree.SubElement(ip, "dhcp")
        ElementTree.SubElement(
            dhcp,
            "range",
            {"start": f"10.200.{subnet}.2", "end": f"10.200.{subnet}.254"},
        )
        return ElementTree.tostring(network, encoding="unicode")

    @staticmethod
    def _cloud_init(
        vm: VMDefinition,
        user_key: str,
        host_private: str,
        host_public: str,
        *,
        extra_bypass: tuple[str, ...] = (),
    ) -> str:
        setup = vm.setup.read_text(encoding="utf-8")
        document = {
            "ssh_pwauth": False,
            "disable_root": True,
            "users": [
                {
                    "name": vm.ssh_user or "student",
                    "lock_passwd": True,
                    "sudo": "ALL=(ALL) NOPASSWD:ALL",
                    "ssh_authorized_keys": [user_key.strip()],
                }
            ],
            "ssh_deletekeys": True,
            "ssh_keys": {
                "ed25519_private": host_private,
                "ed25519_public": host_public.strip(),
            },
            "write_files": [
                {
                    "path": "/usr/local/sbin/labctl-setup",
                    "permissions": "0700",
                    "owner": "root:root",
                    "content": setup,
                }
            ],
            "runcmd": [["/usr/local/sbin/labctl-setup"]],
        }
        configure_proxy(document, extra_bypass=extra_bypass)
        return "#cloud-config\n" + yaml.safe_dump(document, sort_keys=False)

    def create(
        self,
        definition: LabDefinition,
        uri: str,
        images: ImageStore,
        *,
        allow_untrusted: bool = False,
        readiness_probe: ReadinessProbe | None = None,
    ) -> dict[str, Any]:
        """Create all resources and return only after every VM is ready."""
        state_path = self._state_path(definition.id)
        instance = self._instance_path(definition.id)
        resolved: dict[str, tuple[Path, str, bool]] = {}
        try:
            for vm in definition.vms:
                resolved[vm.image] = images.resolve(vm.image)
        except ImageError as exc:
            raise OrchestrationError(str(exc)) from exc
        untrusted = sorted(name for name, (_, _, trusted) in resolved.items() if not trusted)
        if untrusted and not allow_untrusted:
            raise OrchestrationError(
                f"untrusted image requires --allow-untrusted-image: {', '.join(untrusted)}"
            )
        with self._lab_lock(definition.id):
            journal = self._load_journal(definition.id)
            if journal is not None:
                if journal.get("operation") != "create":
                    raise self._pending_transaction_error(definition.id, journal, "creation")
                self._recover_create(journal)
            if state_path.exists():
                raise OrchestrationError(f"lab instance already exists: {definition.id}")
            if instance.exists():
                raise OrchestrationError(f"instance directory already exists: {instance}")
            return self._create_locked(
                definition, uri, resolved, state_path, instance, readiness_probe
            )

    def _create_locked(
        self,
        definition: LabDefinition,
        uri: str,
        resolved: dict[str, tuple[Path, str, bool]],
        state_path: Path,
        instance: Path,
        readiness_probe: ReadinessProbe | None,
    ) -> dict[str, Any]:
        uid = str(uuid.uuid4())
        storage = self.storage_root / uid if self.storage_root is not None else instance
        if storage != instance and storage.exists():
            raise OrchestrationError(f"storage directory already exists: {storage}")
        network = KVMProvider.resource_name(uid, "network")
        state: dict[str, Any] = {
            "schema_version": 1,
            "id": definition.id,
            "provider": "kvm",
            "provider_uri": uri,
            "state": "provisioning",
            "instance_uid": uid,
            "network": network,
            "vm_order": [vm.name for vm in definition.dependency_order()],
            "image_digests": {name: value[1] for name, value in resolved.items()},
            "definition_digest": directory_digest(definition.source.parent),
            "vms": {},
        }
        if storage != instance:
            state["storage_path"] = str(storage)
        rollback: list[tuple[str, str | None, Callable[[], object]]] = []
        rollback_errors: list[str] = []
        instance_created = False
        storage_created = False
        journal_path = self._journal_path(definition.id)
        journal: dict[str, Any] = {
            "schema_version": 1,
            "id": definition.id,
            "operation": "create",
            "provider_uri": uri,
            "instance_uid": uid,
            "instance_owned": True,
            "network": network,
            "vm_order": state["vm_order"],
            "vms": {},
        }
        if storage != instance:
            journal["storage_path"] = str(storage)
            journal["storage_owned"] = True
        save_json(journal_path, journal)
        save_state(state_path, state)
        try:
            logger.debug("creating private instance data for %s", definition.id)
            instance.mkdir(parents=True, mode=0o700)
            instance_created = True
            os.chmod(instance, 0o700)
            if storage != instance:
                self._mkdir_shared(storage)
                storage_created = True
                self._write_storage_marker(storage, definition.id, uid)
                self._fsync_storage_hierarchy(storage)
            snapshot = instance / "definition"
            logger.debug("snapshotting definition for %s", definition.id)
            shutil.copytree(definition.source.parent, snapshot, symlinks=True)
            for path in snapshot.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    path.chmod(0o700 if os.access(path, os.X_OK) else 0o600)
            logger.debug("generating SSH identity for %s", definition.id)
            private, public = self.keygen(instance / "keys")
            state["identity_file"] = str(private)
            network_xml = instance / "network.xml"
            network_xml.write_text(self._network_xml(network, uid), encoding="utf-8")
            if self._resource_exists(uri, "net-info", network, "network"):
                self._verify_network(uri, network, uid)
                raise OrchestrationError(f"network already exists: {network}")
            logger.debug("defining libvirt network %s", network)
            self._virsh(uri, "net-define", str(network_xml))
            rollback.append(
                ("network", network, partial(self._virsh, uri, "net-undefine", network))
            )
            self._verify_network(uri, network, uid)
            logger.debug("starting libvirt network %s", network)
            self._virsh(uri, "net-start", network)
            rollback.append(("operation", None, partial(self._virsh, uri, "net-destroy", network)))
            for vm in definition.dependency_order():
                domain = KVMProvider.resource_name(uid, vm.name)
                logger.debug("preparing VM %s as domain %s", vm.name, domain)
                if self._resource_exists(uri, "dominfo", domain, "domain"):
                    self._verify_domain(uri, domain, uid, vm.name)
                    raise OrchestrationError(f"domain already exists: {domain}")
                control_vm_dir = instance / "vms" / vm.name
                control_vm_dir.mkdir(parents=True, mode=0o700)
                storage_vm_dir = storage / "vms" / vm.name
                if storage_vm_dir != control_vm_dir:
                    storage_vms = storage / "vms"
                    self._mkdir_shared(storage_vms, exist_ok=True)
                    self._mkdir_shared(storage_vm_dir)
                overlay = storage_vm_dir / "disk.qcow2"
                source_base, digest, _trusted = resolved[vm.image]
                base = (
                    self._copy_verified_base(source_base, digest, storage)
                    if storage != instance
                    else source_base
                )
                self.runner.run(
                    [
                        "qemu-img",
                        "create",
                        "-f",
                        "qcow2",
                        "-F",
                        "qcow2",
                        "-b",
                        str(base),
                        str(overlay),
                        str(parse_size(vm.disk)),
                    ]
                )
                if overlay.exists():
                    overlay.chmod(0o660)
                    self._fsync_file(overlay)
                host_private, host_public = self.host_keygen(control_vm_dir / "host-key")
                user_data = control_vm_dir / "user-data"
                user_data.write_text(
                    self._cloud_init(
                        vm,
                        public.read_text(encoding="utf-8"),
                        host_private.read_text(encoding="utf-8"),
                        host_public.read_text(encoding="utf-8"),
                        extra_bypass=(
                            f"10.200.{16 + uuid.uuid5(uuid.NAMESPACE_OID, uid).int % 224}.0/24",
                            *(
                                name
                                for peer in definition.vms
                                for name in (peer.name, peer.hostname)
                            ),
                        ),
                    ),
                    encoding="utf-8",
                )
                user_data.chmod(0o600)
                metadata = control_vm_dir / "meta-data"
                metadata.write_text(
                    f"instance-id: {uid}-{vm.name}\nlocal-hostname: {vm.hostname}\n",
                    encoding="utf-8",
                )
                seed = storage_vm_dir / "seed.iso"
                self.runner.run(["cloud-localds", str(seed), str(user_data), str(metadata)])
                if seed.exists():
                    seed.chmod(0o640)
                    self._fsync_file(seed)
                argv = [
                    "virt-install",
                    "--connect",
                    uri,
                    "--name",
                    domain,
                    "--memory",
                    str(vm.ram_bytes // 1024**2),
                    "--vcpus",
                    str(vm.cpus),
                    "--import",
                    *self._prebuilt_disk_args(overlay, seed),
                    "--network",
                    f"network={network},model=virtio",
                    "--osinfo",
                    "detect=on,require=off",
                    "--noautoconsole",
                    "--wait",
                    "0",
                ]
                vm_state = {
                    "domain": domain,
                    "state": "provisioning",
                    "image": vm.image,
                    "image_digest": digest,
                    "backing_file": str(base),
                    "overlay": str(overlay),
                    "seed": str(seed),
                    "disk_size_bytes": parse_size(vm.disk),
                    "ssh_user": vm.ssh_user or "student",
                    "hostname": vm.hostname,
                    "depends_on": list(vm.depends_on),
                    "identity_file": str(private),
                    "known_hosts": str(control_vm_dir / "known_hosts"),
                    "host_public_key": host_public.read_text(encoding="utf-8").strip(),
                    "virt_install_argv": argv,
                }
                state["vms"][vm.name] = vm_state
                save_state(state_path, state)
                journal["vms"][vm.name] = {
                    "domain": domain,
                    "stage": "virt-install-planned",
                    "material": [
                        domain,
                        int(argv[6]) * 1024**2,
                        int(argv[8]),
                        [["disk", str(overlay)], ["cdrom", str(seed)]],
                        "network",
                        network,
                        "virtio",
                    ],
                }
                save_json(journal_path, journal)
                rollback.append(
                    (
                        "domain",
                        vm.name,
                        partial(self._cleanup_created_domain, uri, domain, uid, vm.name),
                    )
                )
                logger.debug("installing VM domain %s", domain)
                self.runner.run(argv)
                ownership = self._ownership_xml(uid, "domain", vm.name)
                self._virsh(
                    uri,
                    "metadata",
                    domain,
                    "--config",
                    "--live",
                    "--key",
                    "labctl",
                    "--uri",
                    "urn:labctl",
                    "--set",
                    ownership,
                )
                self._verify_domain(uri, domain, uid, vm.name)
                journal["vms"][vm.name]["stage"] = "metadata-attached"
                save_json(journal_path, journal)
                vm_state["state"] = "running"
                save_state(state_path, state)
                logger.debug("waiting for VM %s readiness", vm.name)
                address = self._wait_ready(uri, domain, vm_state, readiness_probe)
                logger.debug("VM %s is ready at %s", vm.name, address)
                vm_state["address"] = address
                vm_state["state"] = "ready"
                save_state(state_path, state)
            self._refresh_controller_access(state, readiness_probe)
            logger.debug("committing ready state for lab %s", definition.id)
            self._fsync_tree(instance)
            state["state"] = "ready"
            save_state(state_path, state)
            self._unlink_durable(journal_path)
            return state
        except Exception as original:
            cleanup = state.setdefault("cleanup", {})
            for kind, resource, undo in reversed(rollback):
                try:
                    undo()
                    if kind == "domain" and resource is not None:
                        state["vms"][resource]["state"] = "missing"
                    elif kind == "network":
                        cleanup["network"] = "missing"
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if not rollback_errors and storage != instance and storage_created:
                try:
                    self._validate_storage_marker(storage, definition.id, uid)
                    self._rmtree_durable(storage)
                    cleanup["storage"] = "missing"
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if not rollback_errors and instance_created:
                try:
                    self._rmtree_durable(instance)
                    cleanup["instance"] = "missing"
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if not rollback_errors:
                try:
                    self._unlink_durable(state_path)
                    self._unlink_durable(journal_path)
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if rollback_errors:
                state["state"] = "failed_cleanup"
                state["cleanup_failures"] = rollback_errors
                save_state(state_path, state)
            detail = f"create failed: {original}"
            if rollback_errors:
                detail += "; rollback failures: " + "; ".join(rollback_errors)
            self._log_failure(definition.id, detail)
            raise OrchestrationError(detail) from original

    def _controller_access_definition(
        self, state: dict[str, Any]
    ) -> ControllerAccessDefinition | None:
        # No prefix implicitly enables access. Existing snapshots without the
        # opt-in remain unchanged, including already-created Ansible labs.
        if not str(state["id"]).startswith("AN") or "definition_digest" not in state:
            return None
        snapshot = self._instance_path(str(state["id"])) / "definition"
        with self._open_directory_no_symlinks(snapshot):
            if (snapshot / "lab.yaml").is_symlink() or directory_digest(snapshot) != state[
                "definition_digest"
            ]:
                raise OrchestrationError("practice access definition snapshot is altered")
            definition = load_definition(snapshot / "lab.yaml")
        if definition.id != state["id"]:
            raise OrchestrationError("practice access definition identity mismatch")
        access = definition.controller_access
        if access is not None:
            instance = self._instance_path(str(state["id"]))
            for name in [access.controller, *(name for name, _ in access.targets)]:
                vm = state["vms"].get(name)
                if vm is None:
                    continue
                controller_access.public_key(vm["host_public_key"])
                for field, expected in (
                    ("identity_file", instance / "keys/id_lab"),
                    ("known_hosts", instance / "vms" / name / "known_hosts"),
                ):
                    if vm.get(field) != str(expected):
                        raise OrchestrationError("unsafe practice SSH control path")
                    with self._open_directory_no_symlinks(expected.parent) as parent:
                        try:
                            fd = os.open(
                                expected.name,
                                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=parent,
                            )
                        except OSError as exc:
                            raise OrchestrationError("unsafe practice SSH control file") from exc
                        try:
                            info = os.fstat(fd)
                            if (
                                not stat.S_ISREG(info.st_mode)
                                or info.st_nlink != 1
                                or info.st_uid != os.getuid()
                                or info.st_mode & 0o077
                            ):
                                raise OrchestrationError("unsafe practice SSH control file")
                        finally:
                            os.close(fd)
        return definition.controller_access

    def _practice_remote(self, vm: dict[str, Any], script: str, *, operation: str) -> str:
        if vm.get("ssh_user") != "student":
            raise OrchestrationError("practice access requires student SSH user")
        command = build_ssh_command(
            "student",
            str(vm["address"]),
            Path(vm["identity_file"]),
            Path(vm["known_hosts"]),
            ("/usr/bin/python3 -c " + shlex.quote(script),),
        )
        # Bound both connection and remote work, including a wedged SSH peer.
        command[1:1] = [
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=3",
        ]
        result = self.runner.run(
            ["timeout", "--signal=TERM", "--kill-after=5", "60", *command], check=False
        )
        if result.returncode:
            raise OrchestrationError(
                f"practice access {operation} failed for VM {vm['domain']} "
                f"(exit {result.returncode})"
            )
        return result.stdout

    def _refresh_controller_access(
        self, state: dict[str, Any], readiness_probe: ReadinessProbe | None
    ) -> None:
        access = self._controller_access_definition(state)
        if access is None:
            return
        names = [access.controller, *(name for name, _ in access.targets)]
        vms = state["vms"]
        uri = str(state["provider_uri"])
        # Never broaden a selective lifecycle operation by powering on peers.
        # A later start refreshes the complete configuration when all are up.
        if any(name not in vms for name in names):
            state["controller_access_status"] = "pending"
            return
        for name in names:
            self._verify_domain(uri, str(vms[name]["domain"]), self._uid(state), name)
        if not all(self._domain_active(uri, str(vms[name]["domain"])) for name in names):
            state["controller_access_status"] = "pending"
            return
        state["controller_access_status"] = "pending"
        for name in names:
            vm = vms[name]
            vm["address"] = self._wait_ready(uri, str(vm["domain"]), vm, readiness_probe)
        # Validate *all* target data before generating a key or authorizing it.
        script = controller_access.controller_script(access, vms)
        public = self._practice_remote(
            vms[access.controller], controller_access.key_script(), operation="prepare key"
        )
        authorize = controller_access.authorize_script(public.strip())
        for name, _ in access.targets:
            self._practice_remote(vms[name], authorize, operation="authorize key")
        self._practice_remote(vms[access.controller], script, operation="publish connections")
        state["controller_access_status"] = "ready"

    def _wait_ready(
        self,
        uri: str,
        domain: str,
        vm: dict[str, Any],
        readiness_probe: ReadinessProbe | None,
    ) -> str:
        deadline = time.monotonic() + self.address_timeout
        address = ""
        # Bound the actual child, not just retries. GNU timeout preserves the
        # injectable Runner API; KILL leaves no unbounded termination grace.
        while (remaining := deadline - time.monotonic()) > 0:
            result = self.runner.run(
                [
                    "timeout",
                    "--signal=KILL",
                    str(remaining),
                    "virsh",
                    "--connect",
                    uri,
                    "domifaddr",
                    domain,
                    "--source",
                    "lease",
                ],
                check=False,
            )
            for field in result.stdout.split():
                if "/" in field and field[0].isdigit():
                    address = field.split("/", 1)[0]
                    break
            if address:
                break
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        if not address:
            raise OrchestrationError(f"readiness phase timed out: address for {domain}")
        write_known_host(address, str(vm["host_public_key"]), Path(str(vm["known_hosts"])))
        if readiness_probe is not None:
            if not readiness_probe(domain, address):
                raise OrchestrationError(f"readiness phase failed: SSH/cloud-init for {domain}")
            return address
        common = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={vm['known_hosts']}",
            "-o",
            "ConnectTimeout=5",
            "-i",
            str(vm["identity_file"]),
            "--",
            f"{vm['ssh_user']}@{address}",
        ]
        deadline = time.monotonic() + self.ssh_timeout
        while (remaining := deadline - time.monotonic()) > 0:
            if (
                self.runner.run(
                    ["timeout", "--signal=KILL", str(remaining), *common, "true"], check=False
                ).returncode
                == 0
            ):
                break
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        else:
            raise OrchestrationError(f"readiness phase timed out: SSH for {domain}")
        deadline = time.monotonic() + self.cloud_init_timeout
        while (remaining := deadline - time.monotonic()) > 0:
            result = self.runner.run(
                [
                    "timeout",
                    "--signal=KILL",
                    str(remaining),
                    *common,
                    "sudo -n cloud-init status --wait --long",
                ],
                check=False,
            )
            if result.returncode == 0:
                return address
            # GNU timeout killed by its own process-group signal is -9 through
            # subprocess, or 137 through a shell. SSH transport failures are 255.
            if result.returncode not in (-9, 124, 137, 255):
                # Exit 1 can be either cloud-init failure or sudo refusal. Do not
                # guess from (or disclose) captured output, which may hold secrets.
                raise OrchestrationError(
                    f"readiness phase failed: cloud-init for {domain} "
                    f"(exit {result.returncode}); check cloud-init and passwordless sudo"
                )
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise OrchestrationError(f"readiness phase timed out: cloud-init for {domain}")

    def stop(
        self,
        lab_id: str,
        *,
        force: bool,
        wait_stopped: WaitStopped | None = None,
    ) -> dict[str, Any]:
        with self._lab_lock(lab_id):
            self._require_no_pending_transaction(lab_id, "stop")
            return self._stop_locked(lab_id, force=force, wait_stopped=wait_stopped)

    def _stop_locked(
        self,
        lab_id: str,
        *,
        force: bool,
        wait_stopped: WaitStopped | None = None,
        vm_name: str | None = None,
    ) -> dict[str, Any]:
        state_path, state = self._load(lab_id)
        uri = str(state["provider_uri"])
        vms: dict[str, dict[str, Any]] = state.get("vms", {})
        order = list(state.get("vm_order", vms))
        if vm_name is not None:
            order = self._dependant_order(vms, order, vm_name)
        order = [name for name in order if vms[name].get("state") != "missing"]
        order = list(reversed(order))
        before = self._observe_power(uri, state, order)
        waiter = wait_stopped or (lambda domain, timeout: self._wait_stopped(uri, domain, timeout))
        try:
            for name in order:
                if not before[name]:
                    continue
                domain = str(vms[name]["domain"])
                self._verify_domain(uri, domain, self._uid(state), name)
                self._virsh(uri, "shutdown", domain)
                if not waiter(domain, self.shutdown_timeout):
                    if not force:
                        raise OrchestrationError(
                            f"{name}: graceful shutdown timed out; retry with --force"
                        )
                    self._verify_domain(uri, domain, self._uid(state), name)
                    self._virsh(uri, "destroy", domain)
            self._set_power_state(state, {name: False for name in order}, before)
            save_state(state_path, state)
            return state
        except Exception as original:
            restoration = self._restore_power(uri, state, order, before)
            self._set_power_state(state, before, before)
            try:
                save_state(state_path, state)
            except Exception as error:
                restoration.append(str(error))
            detail = f"stop failed: {original}"
            if restoration:
                detail += "; restoration failures: " + "; ".join(restoration)
            raise OrchestrationError(detail) from original

    def _wait_stopped(self, uri: str, domain: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self._virsh(uri, "domstate", domain, check=False).strip().lower()
            if state in {"shut off", "shutoff", "crashed"}:
                return True
            time.sleep(1)
        return False

    def _load(self, lab_id: str, *, validate_storage: bool = True) -> tuple[Path, dict[str, Any]]:
        self._validate_lab_id(lab_id)
        path = self._state_path(lab_id)
        if not path.is_file():
            raise OrchestrationNotFoundError(f"lab not found: {lab_id}")
        state = load_state(path)
        if state.get("id") != lab_id:
            raise OrchestrationError("lab state identity does not match its filename")
        self._storage_instance(state, marker=validate_storage)
        vms = state.get("vms", {})
        if isinstance(vms, dict):
            for name in vms:
                self._validate_vm_name(name)
        return path, state

    @staticmethod
    def _prebuilt_disk_args(overlay: Path, seed: Path) -> list[str]:
        # virtinst's path= (including source.file=) registers unowned directory
        # pools. Attach our already-created media only in the final domain XML,
        # outside virtinst's storage discovery/creation machinery.
        argv = ["--disk", "none"]
        for index, path, device, fmt, target, bus in (
            (1, overlay, "disk", "qcow2", "vda", "virtio"),
            (2, seed, "cdrom", "raw", "sda", "sata"),
        ):
            prefix = f"./devices/disk[{index}]"
            for attribute, value in (
                ("/@type", "file"),
                ("/@device", device),
                ("/driver/@name", "qemu"),
                ("/driver/@type", fmt),
                ("/source/@file", str(path)),
                ("/target/@dev", target),
                ("/target/@bus", bus),
            ):
                # XMLManualAction splits shorthand at the LAST '='. Keep the
                # XPath fixed and quote the separate value for virtinst's
                # comma-delimited POSIX shlex parser (not for a shell).
                quoted = "'" + value.replace("'", "'\"'\"'") + "'"
                argv.extend(["--xml", f"xpath.set={prefix}{attribute},xpath.value={quoted}"])
        argv.extend(["--xml", "xpath.create=./devices/disk[2]/readonly"])
        return argv

    def _validated_recreation_argv(
        self,
        lab_id: str,
        name: str,
        state: dict[str, Any],
        vm: dict[str, Any],
    ) -> list[str]:
        """Validate the complete persisted virt-install record before executing it."""
        self._validate_vm_name(name)
        uri = state.get("provider_uri")
        uid = self._uid(state)
        network = state.get("network")
        domain = vm.get("domain")
        argv = vm.get("virt_install_argv")
        storage = self._storage_instance(state).resolve()
        overlay = (storage / "vms" / name / "disk.qcow2").resolve()
        seed = (storage / "vms" / name / "seed.iso").resolve()
        if (
            not isinstance(uri, str)
            or not uri
            or network != KVMProvider.resource_name(uid, "network")
            or domain != KVMProvider.resource_name(uid, name)
            or vm.get("overlay") != str(overlay)
            or vm.get("seed") != str(seed)
            or not isinstance(argv, list)
            or not all(isinstance(item, str) for item in argv)
            or len(argv) < 10
        ):
            raise OrchestrationError(f"invalid recreation record for {name}")
        memory, vcpus = argv[6], argv[8]
        if not memory.isdecimal() or int(memory) <= 0 or not vcpus.isdecimal() or int(vcpus) <= 0:
            raise OrchestrationError(f"invalid recreation record for {name}")
        expected = [
            "virt-install",
            "--connect",
            uri,
            "--name",
            domain,
            "--memory",
            memory,
            "--vcpus",
            vcpus,
            "--import",
            "--disk",
            f"path={overlay},format=qcow2",
            "--disk",
            f"path={seed},device=cdrom",
            "--network",
            f"network={network},model=virtio",
            "--osinfo",
            "detect=on,require=off",
            "--noautoconsole",
            "--wait",
            "0",
        ]
        safe = expected[:10] + self._prebuilt_disk_args(overlay, seed) + expected[14:]
        # The initial pool-free implementation persisted shorthand XML on
        # successful creates. Reconstruct that exact form from OUR generated
        # argv, never parse or execute XPath supplied by a recreation record.
        shorthand = [
            shlex.split(item)[0].removeprefix("xpath.set=").replace(",xpath.value=", "=", 1)
            if item.startswith("xpath.set=")
            else item
            for item in safe
        ]
        # Accept only complete known records; always execute the encoded form.
        if argv not in (expected, shorthand, safe):
            raise OrchestrationError(f"invalid recreation record for {name}")
        return safe

    @staticmethod
    def _validated_backing_blob(images: ImageStore, digest: object) -> Path:
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise OrchestrationError("creation-time image digest must be a lowercase SHA-256")
        candidate = images.blobs / digest
        if candidate.is_symlink():
            raise OrchestrationError(f"creation-time image blob must be a non-symlink: {digest}")
        try:
            mode = candidate.stat().st_mode
            resolved = candidate.resolve(strict=True)
            blob_root = images.blobs.resolve(strict=True)
        except OSError as exc:
            raise OrchestrationError(f"creation-time image blob is missing: {digest}") from exc
        if not resolved.is_relative_to(blob_root):
            raise OrchestrationError(f"creation-time image blob escapes blob directory: {digest}")
        if not stat.S_ISREG(mode):
            raise OrchestrationError(f"creation-time image blob must be regular: {digest}")
        actual = hashlib.sha256()
        with candidate.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                actual.update(chunk)
        if actual.hexdigest() != digest:
            raise OrchestrationError(f"creation-time image blob checksum mismatch: {digest}")
        return candidate

    def _validated_instance_backing(self, state: dict[str, Any], vm: dict[str, Any]) -> Path:
        digest = vm.get("image_digest")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise OrchestrationError("creation-time image digest must be a lowercase SHA-256")
        storage = self._storage_instance(state)
        candidate = storage / "bases" / digest
        if vm.get("backing_file") != str(candidate) or candidate.is_symlink():
            raise OrchestrationError("creation-time image backing path is invalid")
        try:
            with self._open_directory_no_symlinks(candidate.parent) as parent:
                metadata = os.stat(candidate.name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    raise OrchestrationError("creation-time image backing must be regular")
                descriptor = os.open(
                    candidate.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent,
                )
                try:
                    actual = hashlib.sha256()
                    while chunk := os.read(descriptor, 1024 * 1024):
                        actual.update(chunk)
                finally:
                    os.close(descriptor)
        except FileNotFoundError as exc:
            raise OrchestrationError("creation-time image backing is missing") from exc
        if actual.hexdigest() != digest:
            raise OrchestrationError("creation-time image backing checksum mismatch")
        return candidate

    @staticmethod
    def _validated_disk_size(vm: dict[str, Any]) -> str:
        value = vm.get("disk_size_bytes")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise OrchestrationError("creation-time VM disk size is missing or invalid")
        return str(value)

    def _owned_vm_path(self, state: dict[str, Any], name: str, field: str, value: object) -> Path:
        filenames = {"overlay": "disk.qcow2", "seed": "seed.iso"}
        expected = self._storage_instance(state) / "vms" / name / filenames[field]
        if not isinstance(value, str) or Path(value) != expected:
            raise OrchestrationError(f"invalid {field} path for {name}")
        return expected

    @staticmethod
    @contextmanager
    def _open_directory_no_symlinks(path: Path) -> Iterator[int]:
        absolute = Path(os.path.abspath(path))
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        components = absolute.parts[1:]
        try:
            for index, component in enumerate(components):
                flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                flags |= os.O_RDONLY if index == len(components) - 1 else os.O_PATH
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except OSError as exc:
                    raise OrchestrationError(
                        f"unsafe directory component (symlink or non-directory): {path}"
                    ) from exc
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    def _unlink_owned_vm_file(
        self, state: dict[str, Any], name: str, field: str, value: object
    ) -> Path:
        target = self._owned_vm_path(state, name, field, value)
        with self._open_directory_no_symlinks(target.parent) as parent:
            try:
                metadata = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                os.fsync(parent)
                return target
            if stat.S_ISLNK(metadata.st_mode):
                raise OrchestrationError(f"invalid {field} path for {name}: symlink")
            os.unlink(target.name, dir_fd=parent)
            os.fsync(parent)
        return target

    def _validated_reset_vm_path(
        self,
        state: dict[str, Any],
        name: str,
        field: str,
        value: object,
        *,
        required: bool = True,
    ) -> Path:
        target = self._owned_vm_path(state, name, field, value)
        instance = self._storage_instance(state)
        for parent in (target.parent, target.parent.parent, instance):
            if parent.is_symlink():
                raise OrchestrationError(f"invalid {field} path for {name}: symlinked directory")
        if target.is_symlink():
            raise OrchestrationError(f"invalid {field} path for {name}: symlink")
        if required and (not target.exists() or not target.is_file()):
            raise OrchestrationError(f"invalid {field} path for {name}: not a regular file")
        return target

    def _domain_active(self, uri: str, domain: str) -> bool:
        observed = self._virsh(uri, "domstate", domain).strip().lower()
        return observed not in {"shut off", "shutoff", "crashed"}

    def _network_active(self, uri: str, network: str) -> bool:
        for line in self._virsh(uri, "net-info", network).splitlines():
            field, separator, value = line.partition(":")
            if separator and field.strip().lower() == "active":
                observed = value.strip().lower()
                if observed in {"yes", "no"}:
                    return observed == "yes"
        raise OrchestrationError(f"network activity is indeterminate: {network}")

    def _observe_power(self, uri: str, state: dict[str, Any], order: list[str]) -> dict[str, bool]:
        uid = self._uid(state)
        vms: dict[str, dict[str, Any]] = state["vms"]
        observed: dict[str, bool] = {}
        for name in order:
            domain = str(vms[name]["domain"])
            self._verify_domain(uri, domain, uid, name)
            observed[name] = self._domain_active(uri, domain)
        return observed

    def _restore_power(
        self,
        uri: str,
        state: dict[str, Any],
        order: list[str],
        before: dict[str, bool],
    ) -> list[str]:
        failures: list[str] = []
        uid = self._uid(state)
        vms: dict[str, dict[str, Any]] = state["vms"]
        for name in reversed(order):
            domain = str(vms[name]["domain"])
            try:
                active = self._domain_active(uri, domain)
                if active == before[name]:
                    continue
                self._verify_domain(uri, domain, uid, name)
                self._virsh(uri, "start" if before[name] else "destroy", domain)
            except Exception as error:
                failures.append(f"{name}: {error}")
        return failures

    @staticmethod
    def _set_power_state(
        state: dict[str, Any], observed: dict[str, bool], before: dict[str, bool]
    ) -> None:
        vms: dict[str, dict[str, Any]] = state["vms"]
        for name, active in observed.items():
            vms[name]["state"] = "ready" if active else "stopped"
        active_values = [vm.get("state") == "ready" for vm in vms.values()]
        state["state"] = (
            "ready"
            if active_values and all(active_values)
            else "stopped"
            if not any(active_values)
            else "degraded"
        )

    @staticmethod
    def _start_order(
        vms: dict[str, dict[str, Any]], order: list[str], vm_name: str | None
    ) -> list[str]:
        if vm_name is None:
            return order
        if vm_name not in vms:
            raise OrchestrationNotFoundError(f"VM not found: {vm_name}")
        required: set[str] = set()

        def add(name: str) -> None:
            if name in required:
                return
            vm = vms.get(name)
            if vm is None:
                raise OrchestrationError(f"VM {name} has an unknown dependency")
            dependencies = vm.get("depends_on", [])
            if not isinstance(dependencies, list) or not all(
                isinstance(dependency, str) for dependency in dependencies
            ):
                raise OrchestrationError(f"VM {name} has invalid dependency state")
            for dependency in dependencies:
                add(dependency)
            required.add(name)

        add(vm_name)
        return [name for name in order if name in required]

    @staticmethod
    def _dependant_order(
        vms: dict[str, dict[str, Any]], order: list[str], vm_name: str
    ) -> list[str]:
        """Return a target and all transitive dependants in topological order."""
        if vm_name not in vms:
            raise OrchestrationNotFoundError(f"VM not found: {vm_name}")
        if len(order) != len(set(order)) or set(order) != set(vms):
            raise OrchestrationError("lab state has invalid VM order")
        dependants: dict[str, set[str]] = {name: set() for name in order}
        positions = {name: index for index, name in enumerate(order)}
        for name, vm in vms.items():
            dependencies = vm.get("depends_on", [])
            if not isinstance(dependencies, list) or not all(
                isinstance(dependency, str) for dependency in dependencies
            ):
                raise OrchestrationError(f"VM {name} has invalid dependency state")
            if len(dependencies) != len(set(dependencies)):
                raise OrchestrationError(f"VM {name} has duplicate dependencies")
            for dependency in dependencies:
                if dependency not in vms or dependency == name:
                    raise OrchestrationError(f"VM {name} has an unknown dependency")
                if positions[dependency] >= positions[name]:
                    raise OrchestrationError("lab state has invalid dependency order")
                dependants[dependency].add(name)
        required = {vm_name}
        pending = [vm_name]
        while pending:
            current = pending.pop()
            for dependant in dependants[current]:
                if dependant not in required:
                    required.add(dependant)
                    pending.append(dependant)
        return [name for name in order if name in required]

    def start(
        self,
        lab_id: str,
        *,
        readiness_probe: ReadinessProbe | None = None,
        vm_name: str | None = None,
    ) -> dict[str, Any]:
        """Start dependencies before dependants and restore prior power on failure."""
        with self._lab_lock(lab_id):
            self._require_no_pending_transaction(lab_id, "start")
            return self._start_locked(lab_id, readiness_probe=readiness_probe, vm_name=vm_name)

    def _start_locked(
        self,
        lab_id: str,
        *,
        readiness_probe: ReadinessProbe | None = None,
        vm_name: str | None = None,
    ) -> dict[str, Any]:
        path, state = self._load(lab_id)
        uri = str(state["provider_uri"])
        vms: dict[str, dict[str, Any]] = state["vms"]
        order = self._start_order(vms, list(state.get("vm_order", vms)), vm_name)
        self._controller_access_definition(state)
        before = self._observe_power(uri, state, order)
        try:
            for name in order:
                vm = vms[name]
                if before[name]:
                    vm["state"] = "ready"
                    continue
                self._verify_domain(uri, str(vm["domain"]), self._uid(state), name)
                self._virsh(uri, "start", str(vm["domain"]))
                vm["state"] = "running"
                vm["address"] = self._wait_ready(
                    uri,
                    str(vm["domain"]),
                    vm,
                    readiness_probe,
                )
                vm["state"] = "ready"
            state["state"] = (
                "ready" if all(vm.get("state") == "ready" for vm in vms.values()) else "degraded"
            )
            self._refresh_controller_access(state, readiness_probe)
            save_state(path, state)
            return state
        except Exception as original:
            restoration = self._restore_power(uri, state, order, before)
            self._set_power_state(state, before, before)
            try:
                save_state(path, state)
            except Exception as error:
                restoration.append(str(error))
            detail = f"start failed: {original}"
            if restoration:
                detail += "; restoration failures: " + "; ".join(restoration)
            raise OrchestrationError(detail) from original

    def stop_vm(
        self,
        lab_id: str,
        vm_name: str,
        *,
        force: bool,
        wait_stopped: WaitStopped | None = None,
    ) -> dict[str, Any]:
        with self._lab_lock(lab_id):
            self._require_no_pending_transaction(lab_id, "VM stop")
            return self._stop_locked(
                lab_id, force=force, wait_stopped=wait_stopped, vm_name=vm_name
            )

    def restart(
        self,
        lab_id: str,
        *,
        force: bool,
        readiness_probe: ReadinessProbe | None = None,
        vm_name: str | None = None,
    ) -> dict[str, Any]:
        with self._lab_lock(lab_id):
            self._require_no_pending_transaction(lab_id, "restart")
            path, state = self._load(lab_id)
            self._controller_access_definition(state)
            uri = str(state["provider_uri"])
            vms: dict[str, dict[str, Any]] = state["vms"]
            order = list(state.get("vm_order", vms))
            if vm_name is not None:
                if vm_name not in vms:
                    raise OrchestrationNotFoundError(f"VM not found: {lab_id}/{vm_name}")
                order = [vm_name]
            before = self._observe_power(uri, state, order)
            try:
                for name in reversed(order):
                    if not before[name]:
                        continue
                    domain = str(vms[name]["domain"])
                    self._verify_domain(uri, domain, self._uid(state), name)
                    self._virsh(uri, "shutdown", domain)
                    if not self._wait_stopped(uri, domain, self.shutdown_timeout):
                        if not force:
                            raise OrchestrationError(
                                f"{name}: graceful shutdown timed out; retry with --force"
                            )
                        self._verify_domain(uri, domain, self._uid(state), name)
                        self._virsh(uri, "destroy", domain)
                for name in order:
                    if not before[name]:
                        continue
                    domain = str(vms[name]["domain"])
                    self._verify_domain(uri, domain, self._uid(state), name)
                    self._virsh(uri, "start", domain)
                    self._wait_ready(uri, domain, vms[name], readiness_probe)
                self._set_power_state(state, before, before)
                self._refresh_controller_access(state, readiness_probe)
                save_state(path, state)
                return state
            except Exception as original:
                restoration = self._restore_power(uri, state, order, before)
                self._set_power_state(state, before, before)
                try:
                    save_state(path, state)
                except Exception as error:
                    restoration.append(str(error))
                detail = f"restart failed: {original}"
                if restoration:
                    detail += "; restoration failures: " + "; ".join(restoration)
                raise OrchestrationError(detail) from original

    def _verify_domain(self, uri: str, domain: str, uid: str, vm_name: str) -> None:
        observed = self._virsh(
            uri, "metadata", domain, "--key", "labctl", "--uri", "urn:labctl", check=False
        ).strip()
        if self._parse_ownership(observed) != self._ownership(uid, "domain", vm_name):
            raise OrchestrationError(f"ownership verification failed for domain {domain}")

    def _verify_network(self, uri: str, network: str, uid: str) -> None:
        observed = self._virsh(uri, "net-dumpxml", network, check=False)
        if self._parse_ownership(observed, network=True) != self._ownership(uid, "network"):
            raise OrchestrationError(f"ownership verification failed for network {network}")

    @staticmethod
    def _network_material(xml: str) -> tuple[object, ...] | None:
        try:
            root = SafeElementTree.fromstring(xml)
        except (ElementTree.ParseError, DefusedXmlException):
            return None
        forward = root.find("forward")
        bridge = root.find("bridge")
        ip = root.find("ip")
        dhcp_range = root.find("ip/dhcp/range")
        return (
            root.findtext("name"),
            forward.get("mode") if forward is not None else None,
            bridge.get("name") if bridge is not None else None,
            ip.get("address") if ip is not None else None,
            ip.get("netmask") if ip is not None else None,
            dhcp_range.get("start") if dhcp_range is not None else None,
            dhcp_range.get("end") if dhcp_range is not None else None,
        )

    @staticmethod
    def _domain_material(xml: str) -> tuple[object, ...] | None:
        try:
            root = SafeElementTree.fromstring(xml)
        except (ElementTree.ParseError, DefusedXmlException):
            return None
        memory = root.find("memory")
        if memory is None:
            return None
        units = {"b": 1, "bytes": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3}
        try:
            memory_bytes = int(memory.text or "") * units[(memory.get("unit") or "KiB").lower()]
            vcpus = int(root.findtext("vcpu") or "")
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        disks = tuple(
            (disk.get("device"), source.get("file") if source is not None else None)
            for disk in root.findall("devices/disk")
            for source in [disk.find("source")]
        )
        interface = root.find("devices/interface")
        source = interface.find("source") if interface is not None else None
        model = interface.find("model") if interface is not None else None
        return (
            root.findtext("name"),
            memory_bytes,
            vcpus,
            disks,
            interface.get("type") if interface is not None else None,
            source.get("network") if source is not None else None,
            model.get("type") if model is not None else None,
        )

    def remove_vm(self, lab_id: str, vm_name: str, *, force: bool) -> dict[str, Any]:
        self._validate_lab_id(lab_id)
        self._validate_vm_name(vm_name)
        if not force:
            raise OrchestrationError("direct VM removal requires --force")
        with self._lab_lock(lab_id):
            journal = self._load_journal(lab_id)
            if journal is not None and (
                journal.get("operation") != "remove-vm" or journal.get("vm_name") != vm_name
            ):
                raise self._pending_transaction_error(lab_id, journal, "VM removal")
            return self._remove_vm_locked(lab_id, vm_name, journal)

    def _remove_vm_locked(
        self, lab_id: str, vm_name: str, journal: dict[str, Any] | None
    ) -> dict[str, Any]:
        path, state = self._load(lab_id)
        try:
            vm = state["vms"][vm_name]
        except KeyError as exc:
            raise OrchestrationNotFoundError(f"VM not found: {lab_id}/{vm_name}") from exc
        uri = str(state["provider_uri"])
        domain = str(vm["domain"])
        uid = self._uid(state)
        order = list(state.get("vm_order", state["vms"]))
        if vm.get("state") != "missing":
            dependants = [
                name
                for name in self._dependant_order(state["vms"], order, vm_name)
                if name != vm_name and state["vms"][name].get("state") != "missing"
            ]
            if dependants:
                raise OrchestrationError(
                    f"cannot remove {vm_name}; dependent VMs remain: {', '.join(dependants)}"
                )
        expected_files = [
            str(self._owned_vm_path(state, vm_name, field, vm.get(field)))
            for field in ("overlay", "seed")
        ]
        if journal is None:
            journal = {
                "schema_version": 1,
                "id": lab_id,
                "operation": "remove-vm",
                "provider_uri": uri,
                "instance_uid": uid,
                "vm_name": vm_name,
                "domain": domain,
                "domain_stage": "planned",
                "files": {value: "planned" for value in expected_files},
            }
            save_json(self._journal_path(lab_id), journal)
        elif (
            journal.get("schema_version") != 1
            or journal.get("provider_uri") != uri
            or journal.get("instance_uid") != uid
            or journal.get("domain") != domain
            or journal.get("domain_stage") not in {"planned", "missing"}
            or journal.get("files")
            != {
                value: journal.get("files", {}).get(value)
                for value in expected_files
                if isinstance(journal.get("files"), dict)
            }
            or not isinstance(journal.get("files"), dict)
            or set(journal["files"]) != set(expected_files)
            or not all(stage in {"planned", "missing"} for stage in journal["files"].values())
        ):
            raise OrchestrationError("invalid remove-VM transaction")
        if self._resource_exists(uri, "dominfo", domain, "domain"):
            self._verify_domain(uri, domain, uid, vm_name)
            if self._domain_active(uri, domain):
                self._verify_domain(uri, domain, uid, vm_name)
                self._virsh(uri, "destroy", domain)
            self._verify_domain(uri, domain, uid, vm_name)
            self._virsh(uri, "undefine", domain, "--nvram")
        if self._resource_exists(uri, "dominfo", domain, "domain"):
            raise OrchestrationError(f"domain remains after removal: {domain}")
        journal["domain_stage"] = "missing"
        save_json(self._journal_path(lab_id), journal)
        vm["state"] = "missing"
        state["state"] = "degraded"
        save_state(path, state)
        cleanup = state.setdefault("cleanup", {})
        removed_files = cleanup.setdefault("files", [])
        failures: list[str] = []
        for field in ("overlay", "seed"):
            value = vm.get(field)
            target = self._owned_vm_path(state, vm_name, field, value)
            try:
                self._unlink_owned_vm_file(state, vm_name, field, value)
                journal["files"][str(target)] = "missing"
                save_json(self._journal_path(lab_id), journal)
                if value not in removed_files:
                    removed_files.append(value)
                save_state(path, state)
            except Exception as error:
                failures.append(f"{value}: {error}")
        if failures:
            state["state"] = "failed_cleanup"
            state["cleanup_failures"] = failures
            save_state(path, state)
            detail = "cleanup incomplete: " + "; ".join(failures)
            self._log_failure(lab_id, detail)
            raise OrchestrationError(detail)
        state["state"] = "degraded"
        state.pop("cleanup_failures", None)
        save_state(path, state)
        self._unlink_durable(self._journal_path(lab_id))
        return state

    def remove(self, lab_id: str, *, force: bool) -> None:
        with self._lab_lock(lab_id):
            journal = self._load_journal(lab_id)
            if journal is not None and journal.get("operation") == "create":
                self._recover_create(journal)
                if not self._state_path(lab_id).exists():
                    return
                # A committed create only clears its journal; remove the ready lab normally.
                journal = self._load_journal(lab_id)
            if journal is not None and journal.get("operation") != "remove":
                raise self._pending_transaction_error(lab_id, journal, "removal")
            path = self._state_path(lab_id)
            if not path.exists() and journal is not None:
                self._unlink_durable(self._journal_path(lab_id))
                return
            self._remove_locked(lab_id, force=force, journal=journal)

    def _remove_locked(self, lab_id: str, *, force: bool, journal: dict[str, Any] | None) -> None:
        path, state = self._load(lab_id, validate_storage=False)
        vms: dict[str, dict[str, Any]] = state.get("vms", {})
        uri = str(state["provider_uri"])
        uid = self._uid(state)
        order = list(state.get("vm_order", vms))
        network = str(state["network"])
        instance = self._instance_path(lab_id)
        storage = self._storage_instance(state, marker=False)
        split_storage = storage != instance
        if split_storage and storage.exists():
            self._validate_storage_marker(storage, lab_id, uid)
        if journal is None and not force:
            present = []
            for name in order:
                domain = str(vms[name]["domain"])
                if self._resource_exists(uri, "dominfo", domain, "domain"):
                    self._verify_domain(uri, domain, uid, name)
                    present.append(name)
            if any(self._observe_power(uri, state, present).values()):
                raise OrchestrationError("lab is running; stop it first or use --force")
        if journal is None:
            journal = {
                "schema_version": 1,
                "id": lab_id,
                "operation": "remove",
                "provider_uri": uri,
                "instance_uid": uid,
                "network": network,
                "vm_order": order,
                "domains": {name: "planned" for name in order},
                "network_stage": "planned",
                "instance_stage": "planned",
            }
            if split_storage:
                journal["storage_path"] = str(storage)
                journal["storage_stage"] = "planned"
            save_json(self._journal_path(lab_id), journal)
        elif (
            journal.get("schema_version") != 1
            or journal.get("provider_uri") != uri
            or journal.get("instance_uid") != uid
            or journal.get("network") != network
            or journal.get("vm_order") != order
            or not isinstance(journal.get("domains"), dict)
            or set(journal["domains"]) != set(order)
            or not all(stage in {"planned", "missing"} for stage in journal["domains"].values())
            or journal.get("network_stage") not in {"planned", "missing"}
            or journal.get("instance_stage") not in {"planned", "missing"}
            or (
                split_storage
                and (
                    journal.get("storage_path") != str(storage)
                    or journal.get("storage_stage") not in {"planned", "missing"}
                )
            )
            or (not split_storage and "storage_stage" in journal)
        ):
            raise OrchestrationError("invalid removal transaction")
        changed = False
        present_order: list[str] = []
        for name in order:
            vm = vms[name]
            domain = str(vm["domain"])
            if self._resource_exists(uri, "dominfo", domain, "domain"):
                self._verify_domain(uri, domain, uid, name)
                present_order.append(name)
            else:
                if journal["domains"][name] != "missing" or vm.get("state") != "missing":
                    journal["domains"][name] = "missing"
                    vm["state"] = "missing"
                    changed = True
        if changed:
            save_json(self._journal_path(lab_id), journal)
            save_state(path, state)
        before = self._observe_power(uri, state, present_order)
        corrected = False
        for name in present_order:
            if vms[name].get("state") == "missing":
                vms[name]["state"] = "running" if before[name] else "stopped"
                corrected = True
        if corrected:
            save_state(path, state)
        cleanup = state.setdefault("cleanup", {})
        network_present = self._resource_exists(uri, "net-info", network, "network")
        if network_present:
            self._verify_network(uri, network, uid)
        else:
            if journal["network_stage"] != "missing" or cleanup.get("network") != "missing":
                journal["network_stage"] = "missing"
                save_json(self._journal_path(lab_id), journal)
                cleanup["network"] = "missing"
                save_state(path, state)
        running = [name for name, active in before.items() if active]
        if running and not force:
            raise OrchestrationError("lab is running; stop it first or use --force")
        if running:
            self._stop_locked(lab_id, force=True)
            state = load_state(path)
            vms = state.get("vms", {})
            cleanup = state.setdefault("cleanup", {})
        failures: list[str] = []
        for name in reversed(order):
            vm = vms[name]
            domain = str(vm["domain"])
            try:
                if self._resource_exists(uri, "dominfo", domain, "domain"):
                    self._verify_domain(uri, domain, uid, name)
                    self._virsh(uri, "undefine", domain, "--nvram")
                if self._resource_exists(uri, "dominfo", domain, "domain"):
                    raise OrchestrationError(f"domain remains after removal: {domain}")
                journal["domains"][name] = "missing"
                save_json(self._journal_path(lab_id), journal)
                vm["state"] = "missing"
                save_state(path, state)
            except Exception as error:
                failures.append(f"{domain}: {error}")
        if not failures:
            try:
                if self._resource_exists(uri, "net-info", network, "network"):
                    self._verify_network(uri, network, uid)
                    self._virsh(uri, "net-destroy", network, check=False)
                    self._verify_network(uri, network, uid)
                    self._virsh(uri, "net-undefine", network)
                if self._resource_exists(uri, "net-info", network, "network"):
                    raise OrchestrationError(f"network remains after removal: {network}")
                journal["network_stage"] = "missing"
                save_json(self._journal_path(lab_id), journal)
                cleanup["network"] = "missing"
                save_state(path, state)
            except Exception as error:
                failures.append(f"{network}: {error}")
        if not failures and split_storage:
            try:
                if storage.exists():
                    self._validate_storage_marker(storage, lab_id, uid)
                    self._rmtree_durable(storage)
                else:
                    self._fsync_storage_hierarchy(storage)
                if storage.exists():
                    raise OrchestrationError(f"storage remains after removal: {storage}")
                journal["storage_stage"] = "missing"
                save_json(self._journal_path(lab_id), journal)
                cleanup["storage"] = "missing"
                save_state(path, state)
            except Exception as error:
                failures.append(f"{storage}: {error}")
        if not failures:
            try:
                if instance.exists():
                    self._rmtree_durable(instance)
                else:
                    self._fsync_storage_hierarchy(instance)
                if instance.exists():
                    raise OrchestrationError(f"instance remains after removal: {instance}")
                journal["instance_stage"] = "missing"
                save_json(self._journal_path(lab_id), journal)
                cleanup["instance"] = "missing"
                save_state(path, state)
            except Exception as error:
                failures.append(f"{instance}: {error}")
        if failures:
            state["state"] = "failed_cleanup"
            state["cleanup_failures"] = failures
            save_state(path, state)
            self._log_failure(lab_id, "cleanup incomplete: " + "; ".join(failures))
            raise OrchestrationError("cleanup incomplete: " + "; ".join(failures))
        try:
            self._unlink_durable(path)
            self._unlink_durable(self._journal_path(lab_id))
        except Exception as error:
            failures.append(f"{path}: {error}")
            state["state"] = "failed_cleanup"
            state["cleanup_failures"] = failures
            save_state(path, state)
            self._log_failure(lab_id, "cleanup incomplete: " + "; ".join(failures))
            raise OrchestrationError("cleanup incomplete: " + "; ".join(failures)) from error

    def reconcile(self, lab_id: str, *, repair: bool) -> list[dict[str, str]]:
        with self._lab_lock(lab_id):
            self._require_no_pending_transaction(lab_id, "reconcile")
            return self._reconcile_locked(lab_id, repair=repair)

    def _reconcile_locked(self, lab_id: str, *, repair: bool) -> list[dict[str, str]]:
        path, state = self._load(lab_id)
        self._storage_instance(state)
        uri = str(state["provider_uri"])
        uid = self._uid(state)
        actions: list[dict[str, str]] = []
        repaired_missing_network = False
        network = state.get("network")
        if isinstance(network, str):
            if not self._resource_exists(uri, "net-info", network, "network"):
                actions.append(
                    {"resource": network, "action": "mark-missing", "reason": "network absent"}
                )
                if repair:
                    state.setdefault("cleanup", {})["network"] = "missing"
                    repaired_missing_network = True
            else:
                try:
                    self._verify_network(uri, network, uid)
                except OrchestrationError:
                    actions.append(
                        {
                            "resource": network,
                            "action": "manual-recovery",
                            "reason": "foreign or altered ownership",
                        }
                    )
                else:
                    observed_xml = self._virsh(uri, "net-dumpxml", network, check=False)
                    expected_xml = self._network_xml(network, uid)
                    if self._network_material(observed_xml) != self._network_material(expected_xml):
                        actions.append(
                            {
                                "resource": network,
                                "action": "manual-recovery",
                                "reason": "network configuration drift",
                            }
                        )
        for name, vm in state.get("vms", {}).items():
            domain = str(vm["domain"])
            if not self._resource_exists(uri, "dominfo", domain, "domain"):
                actions.append(
                    {"resource": domain, "action": "mark-missing", "reason": "domain absent"}
                )
                if repair:
                    vm["state"] = "missing"
                continue
            try:
                self._verify_domain(uri, domain, uid, name)
            except OrchestrationError:
                actions.append(
                    {
                        "resource": domain,
                        "action": "manual-recovery",
                        "reason": "foreign or altered ownership",
                    }
                )
                continue
            for field in ("overlay", "seed"):
                value = vm.get(field)
                if not isinstance(value, str) or not Path(value).is_file():
                    actions.append(
                        {
                            "resource": domain,
                            "action": "manual-recovery",
                            "reason": f"{field} is missing or altered",
                        }
                    )
            try:
                argv = self._validated_recreation_argv(lab_id, name, state, vm)
            except OrchestrationError as error:
                actions.append(
                    {
                        "resource": domain,
                        "action": "manual-recovery",
                        "reason": f"domain configuration drift: {error}",
                    }
                )
                continue
            observed_domain = self._domain_material(
                self._virsh(uri, "dumpxml", domain, check=False)
            )
            expected_domain = (
                domain,
                int(argv[6]) * 1024**2,
                int(argv[8]),
                (("disk", str(vm["overlay"])), ("cdrom", str(vm["seed"]))),
                "network",
                str(network),
                "virtio",
            )
            if observed_domain != expected_domain:
                actions.append(
                    {
                        "resource": domain,
                        "action": "manual-recovery",
                        "reason": "domain configuration drift",
                    }
                )
            backing = vm.get("backing_file")
            active = self._domain_active(uri, domain)
            try:
                if active:
                    monitor = json.loads(
                        self._virsh(
                            uri,
                            "qemu-monitor-command",
                            domain,
                            '{"execute":"query-block"}',
                            check=False,
                        )
                    )
                    observed = []
                    for block in monitor.get("return", []):
                        inserted = block.get("inserted") if isinstance(block, dict) else None
                        if isinstance(inserted, dict) and inserted.get("file") == vm["overlay"]:
                            observed.append(inserted.get("backing_file"))
                    observed_backing = observed[0] if len(observed) == 1 else None
                else:
                    image_info = self.runner.run(
                        ["qemu-img", "info", "--output=json", str(vm["overlay"])]
                    )
                    observed_backing = json.loads(image_info.stdout).get("backing-filename")
            except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
                observed_backing = None
            if not isinstance(backing, str) or observed_backing != backing:
                actions.append(
                    {
                        "resource": str(vm["overlay"]),
                        "action": "manual-recovery",
                        "reason": "disk backing drift",
                    }
                )
            desired_active = vm.get("state") in {"ready", "running"}
            if active != desired_active:
                actions.append(
                    {
                        "resource": domain,
                        "action": "manual-recovery",
                        "reason": "power state drift",
                    }
                )
        expected_digest = state.get("definition_digest")
        snapshot = self._instance_path(lab_id) / "definition"
        if isinstance(expected_digest, str) and (
            not snapshot.is_dir() or directory_digest(snapshot) != expected_digest
        ):
            actions.append(
                {
                    "resource": str(snapshot),
                    "action": "manual-recovery",
                    "reason": "definition snapshot is missing or altered",
                }
            )
        if repair:
            state["state"] = (
                "failed_cleanup"
                if repaired_missing_network
                else "degraded"
                if actions
                else state.get("state", "ready")
            )
            save_state(path, state)
        return actions

    def reset(
        self,
        lab_id: str,
        images: ImageStore,
        *,
        vm_names: tuple[str, ...] | None = None,
        readiness_probe: ReadinessProbe | None = None,
    ) -> dict[str, Any]:
        """Replace all or selected overlays using creation-time image digests."""
        with self._lab_lock(lab_id):
            journal = self._load_journal(lab_id)
            recovered_selection: tuple[str, ...] | None = None
            if journal is not None:
                if journal.get("operation") != "reset":
                    raise self._pending_transaction_error(lab_id, journal, "reset")
                _lab_id, _phase, _original, _before, records = self._validated_reset_journal(
                    journal
                )
                recovered_selection = tuple(records)
                recovered, committed = self._recover_reset(journal)
                if committed:
                    return recovered
            return self._reset_locked(
                lab_id,
                images,
                vm_names=recovered_selection if recovered_selection is not None else vm_names,
                readiness_probe=readiness_probe,
            )

    @staticmethod
    def _reset_domain_material(
        domain: str,
        argv: list[str],
        overlay: Path,
        seed: Path,
        network: str,
    ) -> list[object]:
        return [
            domain,
            int(argv[6]) * 1024**2,
            int(argv[8]),
            [["disk", str(overlay)], ["cdrom", str(seed)]],
            "network",
            network,
            "virtio",
        ]

    def _validated_reset_journal(
        self, journal: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any], dict[str, bool], dict[str, dict[str, Any]]]:
        lab_id = journal.get("id")
        if not isinstance(lab_id, str):
            raise OrchestrationError("invalid reset transaction: missing lab ID")
        try:
            self._validate_lab_id(lab_id)
        except OrchestrationError as exc:
            raise OrchestrationError("invalid reset transaction: invalid lab ID") from exc
        phase = journal.get("phase")
        if (
            journal.get("schema_version") != 1
            or journal.get("operation") != "reset"
            or phase not in {"rollback", "committed"}
        ):
            raise OrchestrationError("invalid reset transaction: schema, operation, or phase")
        try:
            original = validate_state(journal.get("state"))
        except StateError as exc:
            raise OrchestrationError(f"invalid reset transaction: original state: {exc}") from exc
        if original.get("id") != lab_id:
            raise OrchestrationError("invalid reset transaction: original state lab ID")
        all_order = original.get("vm_order")
        vms = original.get("vms")
        records = journal.get("vms")
        before = journal.get("before")
        selected = journal.get("selected_vms", all_order)
        if (
            not isinstance(all_order, list)
            or not isinstance(selected, list)
            or not selected
            or not all(isinstance(name, str) for name in selected)
            or len(selected) != len(set(selected))
            or any(name not in all_order for name in selected)
            or not isinstance(vms, dict)
            or not isinstance(records, dict)
            or list(records) != selected
            or set(vms) != set(all_order)
            or not isinstance(before, dict)
            or list(before) != selected
            or not all(isinstance(value, bool) for value in before.values())
        ):
            raise OrchestrationError("invalid reset transaction: VM or power records")
        network = original.get("network")
        if not isinstance(network, str):
            raise OrchestrationError("invalid reset transaction: network")
        validated_records: dict[str, dict[str, Any]] = {}
        for name in selected:
            try:
                self._validate_vm_name(name)
            except OrchestrationError as exc:
                raise OrchestrationError("invalid reset transaction: VM name") from exc
            vm = vms[name]
            record = records[name]
            if not isinstance(vm, dict) or not isinstance(record, dict):
                raise OrchestrationError("invalid reset transaction: VM record")
            try:
                overlay = self._validated_reset_vm_path(
                    original, name, "overlay", record.get("overlay"), required=False
                )
            except OrchestrationError as exc:
                raise OrchestrationError(f"invalid reset transaction: {exc}") from exc
            if vm.get("overlay") != str(overlay):
                raise OrchestrationError("invalid reset transaction: overlay state mismatch")
            try:
                seed = self._validated_reset_vm_path(original, name, "seed", vm.get("seed"))
            except OrchestrationError as exc:
                raise OrchestrationError(f"invalid reset transaction: {exc}") from exc
            backup = Path(str(record.get("backup")))
            expected_backup = overlay.with_suffix(".qcow2.reset-backup")
            if (
                backup != expected_backup
                or backup.is_symlink()
                or (backup.exists() and not backup.is_file())
            ):
                raise OrchestrationError("invalid reset transaction: backup path")
            if not overlay.exists() and not backup.is_file():
                raise OrchestrationError("invalid reset transaction: overlay and backup missing")
            if overlay.exists() and (overlay.is_symlink() or not overlay.is_file()):
                raise OrchestrationError("invalid reset transaction: overlay is not regular")
            argv = self._validated_recreation_argv(lab_id, name, original, vm)
            # Old journals carry the fully validated historical record. Accept
            # that exact record too, but execute only its pool-free equivalent.
            if record.get("recreation") not in (argv, vm["virt_install_argv"]):
                raise OrchestrationError("invalid reset transaction: recreation argv")
            domain = vm.get("domain")
            stage = record.get("stage")
            if not isinstance(domain, str) or stage not in {
                "backup-planned",
                "virt-install-planned",
                "metadata-attached",
            }:
                raise OrchestrationError("invalid reset transaction: domain stage")
            material = self._reset_domain_material(domain, argv, overlay, seed, network)
            if stage == "backup-planned":
                if record.get("material") is not None:
                    raise OrchestrationError(
                        "invalid reset transaction: unexpected domain material"
                    )
            elif record.get("material") != material:
                raise OrchestrationError("invalid reset transaction: domain material")
            validated_records[name] = {
                "overlay": overlay,
                "backup": backup,
                "recreation": argv,
                "stage": stage,
                "material": material,
            }
        return lab_id, phase, original, before, validated_records

    def _recover_reset(self, journal: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        lab_id, phase, original, before, records = self._validated_reset_journal(journal)
        path = self._state_path(lab_id)
        if phase == "committed":
            state = load_state(path)
            retained: list[str] = []
            for record in records.values():
                backup = record["backup"]
                try:
                    backup.unlink(missing_ok=True)
                    self._fsync_directory(backup.parent)
                except OSError:
                    retained.append(str(backup))
            cleanup = state.setdefault("cleanup", {})
            if retained:
                cleanup["reset_backups"] = retained
                save_state(path, state)
            else:
                cleanup.pop("reset_backups", None)
                if not cleanup:
                    state.pop("cleanup", None)
                save_state(path, state)
                self._unlink_durable(self._journal_path(lab_id))
            return state, True
        uri = str(original["provider_uri"])
        uid = self._uid(original)
        failures: list[str] = []
        selected = list(records)
        for name in reversed(selected):
            recovery_record = records[name]
            vm = original["vms"][name]
            domain = str(vm["domain"])
            overlay = recovery_record["overlay"]
            backup = recovery_record["backup"]
            try:
                if backup.exists():
                    if self._resource_exists(uri, "dominfo", domain, "domain"):
                        try:
                            self._verify_domain(uri, domain, uid, name)
                        except OrchestrationError as ownership_error:
                            observed = json.loads(
                                json.dumps(
                                    self._domain_material(
                                        self._virsh(uri, "dumpxml", domain, check=False)
                                    )
                                )
                            )
                            if (
                                recovery_record["stage"] != "virt-install-planned"
                                or observed != recovery_record["material"]
                            ):
                                raise OrchestrationError(
                                    f"domain material does not match reset journal for {name}"
                                ) from ownership_error
                            self._virsh(
                                uri,
                                "metadata",
                                domain,
                                "--config",
                                "--live",
                                "--key",
                                "labctl",
                                "--uri",
                                "urn:labctl",
                                "--set",
                                self._ownership_xml(uid, "domain", name),
                            )
                            self._verify_domain(uri, domain, uid, name)
                        self._cleanup_created_domain(uri, domain, uid, name)
                    overlay.unlink(missing_ok=True)
                    backup.replace(overlay)
                    self._fsync_directory(overlay.parent)
                if not self._resource_exists(uri, "dominfo", domain, "domain"):
                    argv = recovery_record["recreation"]
                    self.runner.run(argv)
                    self._virsh(
                        uri,
                        "metadata",
                        domain,
                        "--config",
                        "--live",
                        "--key",
                        "labctl",
                        "--uri",
                        "urn:labctl",
                        "--set",
                        self._ownership_xml(uid, "domain", name),
                    )
            except Exception as exc:
                failures.append(f"{name}: {exc}")
        if failures:
            raise OrchestrationError("reset recovery failed: " + "; ".join(failures))
        power = dict(before)
        failures.extend(self._restore_power(uri, original, selected, power))
        self._set_power_state(original, power, power)
        # Overlay rollback cannot undo credentials/configuration written to
        # unselected peers. Never resurrect the journal's old ready assertion.
        # Recovery stays offline and selective; a later start reconciles access
        # from the restored controller key when the whole topology is running.
        if "controller_access_status" in original:
            original["controller_access_status"] = "pending"
        if failures:
            original["state"] = "degraded"
            original["cleanup_failures"] = failures
        save_state(path, original)
        if failures:
            raise OrchestrationError("reset recovery failed: " + "; ".join(failures))
        self._unlink_durable(self._journal_path(lab_id))
        return original, False

    def _reset_locked(
        self,
        lab_id: str,
        images: ImageStore,
        *,
        vm_names: tuple[str, ...] | None = None,
        readiness_probe: ReadinessProbe | None = None,
    ) -> dict[str, Any]:
        path, state = self._load(lab_id)
        all_order = list(state.get("vm_order", []))
        self._controller_access_definition(state)
        if vm_names is None:
            order = all_order
        else:
            if (
                not vm_names
                or len(vm_names) != len(set(vm_names))
                or any(not isinstance(name, str) for name in vm_names)
            ):
                raise OrchestrationError("invalid reset VM selection")
            for name in vm_names:
                try:
                    self._validate_vm_name(name)
                except OrchestrationError as exc:
                    raise OrchestrationError("invalid reset VM selection") from exc
                if name not in state.get("vms", {}):
                    raise OrchestrationError(f"invalid reset VM selection: unknown VM {name}")
            selected = set(vm_names)
            order = [name for name in all_order if name in selected]
        recreation = {
            name: self._validated_recreation_argv(lab_id, name, state, state["vms"][name])
            for name in order
        }
        reset_paths = {
            name: (
                self._validated_reset_vm_path(
                    state, name, "overlay", state["vms"][name].get("overlay")
                ),
                self._validated_reset_vm_path(state, name, "seed", state["vms"][name].get("seed")),
            )
            for name in order
        }
        backings = {
            name: (
                self._validated_instance_backing(state, state["vms"][name])
                if state.get("storage_path") is not None
                else self._validated_backing_blob(images, state["vms"][name].get("image_digest"))
            )
            for name in order
        }
        disk_sizes = {name: self._validated_disk_size(state["vms"][name]) for name in order}
        before = self._observe_power(str(state["provider_uri"]), state, order)
        journal_path = self._journal_path(lab_id)
        records: dict[str, dict[str, Any]] = {
            name: {
                "overlay": str(reset_paths[name][0]),
                "backup": str(reset_paths[name][0].with_suffix(".qcow2.reset-backup")),
                "recreation": recreation[name],
                "stage": "backup-planned",
                "material": None,
            }
            for name in order
        }
        existing_backup = next(
            (record["backup"] for record in records.values() if Path(record["backup"]).exists()),
            None,
        )
        if existing_backup is not None:
            raise OrchestrationError(f"reset backup already exists: {existing_backup}")
        journal: dict[str, Any] = {
            "schema_version": 1,
            "id": lab_id,
            "operation": "reset",
            "phase": "rollback",
            "state": json.loads(json.dumps(state)),
            "selected_vms": order,
            "before": before,
            "vms": records,
        }
        save_json(journal_path, journal)
        uri = str(state["provider_uri"])
        uid = self._uid(state)
        for name in reversed(order):
            if not before[name]:
                continue
            domain = str(state["vms"][name]["domain"])
            self._verify_domain(uri, domain, uid, name)
            self._virsh(uri, "shutdown", domain)
            if not self._wait_stopped(uri, domain, self.shutdown_timeout):
                self._verify_domain(uri, domain, uid, name)
                self._virsh(uri, "destroy", domain)
        self._set_power_state(state, {name: False for name in order}, before)
        # Persist invalidation before changing guest disks or peer credentials.
        if "controller_access_status" in state:
            state["controller_access_status"] = "pending"
        save_state(path, state)
        state = load_state(path)
        backups: list[tuple[Path, Path]] = []
        recreated: list[tuple[str, dict[str, Any]]] = []
        undefined: list[str] = []
        addresses: dict[str, str] = {}
        try:
            for name in order:
                vm = state["vms"][name]
                domain = str(vm["domain"])
                self._verify_domain(uri, domain, uid, name)
                self._virsh(uri, "undefine", domain, "--nvram")
                undefined.append(name)
                overlay = reset_paths[name][0]
                backup = overlay.with_suffix(".qcow2.reset-backup")
                overlay.replace(backup)
                self._fsync_directory(overlay.parent)
                backups.append((overlay, backup))
                base = backings[name]
                self.runner.run(
                    [
                        "qemu-img",
                        "create",
                        "-f",
                        "qcow2",
                        "-F",
                        "qcow2",
                        "-b",
                        str(base),
                        str(overlay),
                        disk_sizes[name],
                    ]
                )
                if overlay.exists():
                    overlay.chmod(0o660)
                    self._fsync_file(overlay)
                records[name]["stage"] = "virt-install-planned"
                records[name]["material"] = self._reset_domain_material(
                    domain,
                    recreation[name],
                    overlay,
                    reset_paths[name][1],
                    str(state["network"]),
                )
                save_json(journal_path, journal)
                self.runner.run(recreation[name])
                recreated.append((name, vm))
                self._virsh(
                    uri,
                    "metadata",
                    domain,
                    "--config",
                    "--live",
                    "--key",
                    "labctl",
                    "--uri",
                    "urn:labctl",
                    "--set",
                    self._ownership_xml(uid, "domain", name),
                )
                self._verify_domain(uri, domain, uid, name)
                records[name]["stage"] = "metadata-attached"
                save_json(journal_path, journal)
                address = self._wait_ready(uri, domain, vm, readiness_probe)
                addresses[name] = address
                vm["state"] = "ready"
            for name, address in addresses.items():
                state["vms"][name]["address"] = address
            self._set_power_state(state, {name: True for name in order}, before)
            self._refresh_controller_access(state, readiness_probe)
            save_state(path, state)
            journal["phase"] = "committed"
            save_json(journal_path, journal)
        except Exception as original:
            rollback: list[str] = []
            for name, vm in reversed(recreated):
                domain = str(vm["domain"])
                try:
                    if self._domain_active(uri, domain):
                        self._verify_domain(uri, domain, uid, name)
                        self._virsh(uri, "destroy", domain)
                    self._verify_domain(uri, domain, uid, name)
                    self._virsh(uri, "undefine", domain, "--nvram")
                except Exception as error:
                    rollback.append(f"{domain}: {error}")
            for overlay, backup in reversed(backups):
                try:
                    overlay.unlink(missing_ok=True)
                    backup.replace(overlay)
                    self._fsync_directory(overlay.parent)
                except Exception as error:
                    rollback.append(str(error))
            for name in undefined:
                vm = state["vms"][name]
                try:
                    self.runner.run(recreation[name])
                    self._virsh(
                        uri,
                        "metadata",
                        str(vm["domain"]),
                        "--config",
                        "--live",
                        "--key",
                        "labctl",
                        "--uri",
                        "urn:labctl",
                        "--set",
                        self._ownership_xml(uid, "domain", name),
                    )
                except Exception as error:
                    rollback.append(f"{name}: {error}")
            rollback.extend(self._restore_power(uri, state, order, before))
            self._set_power_state(state, before, before)
            # Even a completed refresh is invalid after restoring old disks.
            if "controller_access_status" in state:
                state["controller_access_status"] = "pending"
            if rollback:
                state["state"] = "degraded"
            save_state(path, state)
            detail = f"reset failed: {original}"
            if rollback:
                detail += "; rollback failures: " + "; ".join(rollback)
            self._log_failure(lab_id, detail)
            raise OrchestrationError(detail) from original
        retained: list[str] = []
        for _overlay, backup in backups:
            try:
                backup.unlink()
                self._fsync_directory(backup.parent)
            except OSError:
                retained.append(str(backup))
        cleanup = state.setdefault("cleanup", {})
        if retained:
            cleanup["reset_backups"] = retained
            save_state(path, state)
        else:
            cleanup.pop("reset_backups", None)
            if not cleanup:
                state.pop("cleanup", None)
            save_state(path, state)
            self._unlink_durable(journal_path)
        return state
