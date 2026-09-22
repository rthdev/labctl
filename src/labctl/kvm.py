"""Local KVM/libvirt provider."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from labctl.platform import HostPlatform, detect_host, package_guidance
from labctl.provider import (
    HealthCheck,
    HealthStatus,
    Ownership,
    Provider,
    ProviderCapabilities,
    ProviderHealth,
    ReconcileAction,
    Reconciliation,
)
from labctl.subprocesses import CommandRunner, Runner

_SESSION_URI = "qemu:///session"
_SYSTEM_URI = "qemu:///system"
_PROBES = {
    "compute": ("capabilities",),
    "storage": ("pool-list", "--all"),
    "network": ("net-list", "--all"),
}


class KVMProvider(Provider):
    """Provider using virsh and qemu-img through safe argument vectors."""

    id = "kvm"

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        executable_finder: Callable[[str], str | None] = shutil.which,
        uri: str | None = None,
        platform_detector: Callable[[], HostPlatform] = detect_host,
        storage_path: Path | None = None,
        storage_paths: tuple[Path, ...] | None = None,
        kvm_path: Path = Path("/dev/kvm"),
    ) -> None:
        self._runner = runner or Runner()
        self._executable_finder = executable_finder
        self._uri = uri
        self._platform_detector = platform_detector
        if storage_path is not None and storage_paths is not None:
            raise ValueError("storage_path and storage_paths are mutually exclusive")
        self._storage_paths = storage_paths or (
            storage_path or Path.home() / ".local/share/labctl",
        )
        self._kvm_path = kvm_path

    def _probe(self, uri: str) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for name, arguments in _PROBES.items():
            try:
                result = self._runner.run(["virsh", "--connect", uri, *arguments], check=False)
            except OSError:
                results[name] = False
            else:
                results[name] = result.returncode == 0
        # labctl creates a dedicated NAT network and Linux bridge for every lab.
        # A successful session-mode net-list proves only that the read-only API
        # is available; the unprivileged daemon cannot create that host bridge.
        if uri == _SESSION_URI:
            results["network"] = False
        return results

    @property
    def connection_uri(self) -> str:
        """Prefer unprivileged libvirt only when it provides every required facility."""

        if self._uri is not None:
            return self._uri
        return _SESSION_URI if all(self._probe(_SESSION_URI).values()) else _SYSTEM_URI

    def capabilities(self) -> ProviderCapabilities:
        probe = self._probe(self.connection_uri)
        return ProviderCapabilities(probe["compute"], probe["storage"], probe["network"])

    @staticmethod
    def resource_name(uid: str, resource: str, index: int | None = None) -> str:
        """Build a deterministic libvirt-safe name rooted in the persistent lab UID."""

        clean_uid = re.sub(r"[^a-zA-Z0-9_.-]", "-", uid).strip("-.")
        clean_resource = re.sub(r"[^a-zA-Z0-9_.-]", "-", resource).strip("-.")
        if not clean_uid or not clean_resource:
            raise ValueError("UID and resource must contain name-safe characters")
        suffix = "" if index is None else f"-{index}"
        return f"labctl-{clean_uid}-{clean_resource}{suffix}"

    @staticmethod
    def verify_ownership(metadata: Mapping[str, str], expected: Ownership) -> bool:
        """Require every ownership field; names alone never establish ownership."""

        return all(metadata.get(key) == value for key, value in expected.metadata.items())

    def reconcile(
        self, desired: Ownership, observed_metadata: Mapping[str, str] | None
    ) -> Reconciliation:
        if observed_metadata is None:
            return Reconciliation(ReconcileAction.CREATE, "resource is absent")
        if not self.verify_ownership(observed_metadata, desired):
            return Reconciliation(ReconcileAction.CONFLICT, "resource is not owned by this lab")
        return Reconciliation(ReconcileAction.NONE, "owned resource is present")

    def doctor(self) -> ProviderHealth:
        checks: list[HealthCheck] = []
        try:
            host = self._platform_detector()
            guidance = package_guidance(host).command
        except (OSError, ValueError) as exc:
            checks.append(HealthCheck("platform", HealthStatus.FAIL, str(exc), None))
            guidance = "install libvirt, QEMU, cloud-image-utils, and OpenSSH"
        else:
            checks.append(
                HealthCheck(
                    "platform",
                    HealthStatus.PASS,
                    f"supported {host.distribution} "
                    f"{host.major_version or 'rolling'} {host.architecture}",
                    None,
                )
            )
        for executable in ("virsh", "qemu-img", "virt-install", "cloud-localds", "ssh"):
            path = self._executable_finder(executable)
            checks.append(
                HealthCheck(
                    f"executable:{executable}",
                    HealthStatus.PASS if path else HealthStatus.FAIL,
                    path or f"{executable} was not found",
                    None if path else guidance,
                )
            )

        diagnostic_uri = self._uri or _SESSION_URI
        session = self._probe(diagnostic_uri)
        for capability, label in (
            ("compute", "connectivity"),
            ("storage", "storage"),
            ("network", "network"),
        ):
            available = session[capability]
            checks.append(
                HealthCheck(
                    label,
                    HealthStatus.PASS if available else HealthStatus.WARNING,
                    f"available in {diagnostic_uri}"
                    if available
                    else f"unavailable in {diagnostic_uri}",
                    None
                    if available or self._uri is not None
                    else "labctl will use qemu:///system",
                )
            )
        complete = all(session.values())
        selected_uri = self.connection_uri
        selected = session if selected_uri == diagnostic_uri else self._probe(selected_uri)
        selected_complete = all(selected.values())
        checks.append(
            HealthCheck(
                "permissions",
                HealthStatus.PASS if complete else HealthStatus.WARNING,
                (
                    "unprivileged session is complete"
                    if complete
                    else "system libvirt access is required"
                ),
                None if complete else f"ensure the user is authorized for {self.connection_uri}",
            )
        )
        checks.append(
            HealthCheck(
                "selected-uri",
                HealthStatus.PASS if selected_complete else HealthStatus.FAIL,
                f"all required capabilities available in {selected_uri}"
                if selected_complete
                else f"compute, storage, or isolated NAT unavailable in {selected_uri}",
                None if selected_complete else "authorize libvirt and configure storage/networking",
            )
        )
        kvm_access = os.access(self._kvm_path, os.R_OK | os.W_OK)
        checks.append(
            HealthCheck(
                "kvm-device",
                HealthStatus.PASS if kvm_access else HealthStatus.FAIL,
                f"KVM device is accessible: {self._kvm_path}"
                if kvm_access
                else f"KVM device is unavailable or inaccessible: {self._kvm_path}",
                None
                if kvm_access
                else "enable virtualization and grant the user access to /dev/kvm",
            )
        )
        inaccessible: list[Path] = []
        checked: list[Path] = []
        for storage_path in self._storage_paths:
            if not storage_path.is_dir() or not os.access(
                storage_path, os.R_OK | os.W_OK | os.X_OK
            ):
                inaccessible.append(storage_path)
            elif storage_path not in checked:
                checked.append(storage_path)
        accessible = not inaccessible
        checks.append(
            HealthCheck(
                "storage-access",
                HealthStatus.PASS if accessible else HealthStatus.FAIL,
                "storage paths are accessible: " + ", ".join(str(path) for path in checked)
                if accessible
                else "storage paths are not accessible: "
                + ", ".join(str(path) for path in inaccessible),
                "administrator provisioning is required; current-user access does not prove "
                "QEMU DAC access, and the storage root also requires libvirt-compatible SELinux "
                "labelling",
            )
        )
        try:
            enforcing = self._runner.run(["getenforce"], check=False).stdout.strip() == "Enforcing"
        except OSError:
            enforcing = False
        checks.append(
            HealthCheck(
                "selinux",
                HealthStatus.WARNING if enforcing else HealthStatus.PASS,
                "SELinux is enforcing; verify qemu access to the selected storage"
                if enforcing
                else "no enforcing SELinux mode detected",
                "inspect AVC denials and label storage for libvirt; do not disable SELinux"
                if enforcing
                else None,
            )
        )
        return ProviderHealth(tuple(checks))


PROVIDER_API_VERSION = KVMProvider.api_version
PROVIDER = KVMProvider()
