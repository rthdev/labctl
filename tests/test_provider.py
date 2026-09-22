from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from labctl.kvm import KVMProvider
from labctl.platform import HostPlatform, PackageGuidance, package_guidance, parse_os_release
from labctl.provider import (
    PROVIDER_API_VERSION,
    HealthStatus,
    Ownership,
    Provider,
    ProviderCapabilities,
    ProviderDiscoveryError,
    ReconcileAction,
    Reconciliation,
    discover_providers,
)
from labctl.subprocesses import CommandError, CommandResult, Runner


def test_provider_file_is_inert_until_explicitly_enabled(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    plugin = tmp_path / "example.py"
    plugin.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "from labctl.kvm import KVMProvider\n"
        "PROVIDER_API_VERSION = 1\nPROVIDER = KVMProvider()\n",
        encoding="utf-8",
    )
    assert discover_providers([], search_paths=[tmp_path]) == {}
    assert not marker.exists()
    assert discover_providers(["example"], search_paths=[tmp_path])["kvm"].id == "kvm"
    assert marker.exists()


class FakeRunner:
    def __init__(self, outcomes: dict[tuple[str, ...], CommandResult] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.commands: list[tuple[str, ...]] = []

    def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
        assert isinstance(argv, list)
        command = tuple(argv)
        self.commands.append(command)
        result = self.outcomes.get(command, CommandResult(command, 0, "", ""))
        if check and result.returncode:
            raise CommandError(result)
        return result


class MissingCommandRunner:
    def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
        raise FileNotFoundError(argv[0])


def test_provider_models_are_typed_and_versioned() -> None:
    capabilities = ProviderCapabilities(compute=True, storage=True, network=True)
    owner = Ownership(provider_id="kvm", uid="abc-123", resource_type="domain")
    result = Reconciliation(ReconcileAction.UPDATE, "configuration drift")

    assert PROVIDER_API_VERSION == 1
    assert capabilities.complete
    assert owner.metadata == {
        "labctl.provider": "kvm",
        "labctl.uid": "abc-123",
        "labctl.resource-type": "domain",
    }
    assert result.action is ReconcileAction.UPDATE
    assert Provider.__abstractmethods__ == {"capabilities", "doctor", "reconcile"}


def test_discovery_imports_only_enabled_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "imported"
    (tmp_path / "enabled.py").write_text(
        "from labctl.kvm import KVMProvider\n"
        "from labctl.provider import PROVIDER_API_VERSION\n"
        "PROVIDER = KVMProvider()\n",
        encoding="utf-8",
    )
    (tmp_path / "disabled.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    providers = discover_providers(["enabled"])

    assert providers.keys() == {"kvm"}
    assert not marker.exists()
    assert "disabled" not in sys.modules


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("raise RuntimeError('broken')\n", "could not import"),
        ("PROVIDER = object()\n", "must expose a Provider"),
        (
            "from labctl.kvm import KVMProvider\n"
            "PROVIDER_API_VERSION = 999\nPROVIDER = KVMProvider()\n",
            "API version",
        ),
    ],
)
def test_discovery_reports_invalid_enabled_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, match: str
) -> None:
    (tmp_path / "plugin.py").write_text(body, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ProviderDiscoveryError, match=match):
        discover_providers(["plugin"])
    sys.modules.pop("plugin", None)


def test_discovery_rejects_duplicate_provider_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        "from labctl.kvm import KVMProvider\n"
        "from labctl.provider import PROVIDER_API_VERSION\n"
        "PROVIDER = KVMProvider()\n"
    )
    (tmp_path / "one.py").write_text(body, encoding="utf-8")
    (tmp_path / "two.py").write_text(body, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ProviderDiscoveryError, match="duplicate provider ID 'kvm'"):
        discover_providers(["one", "two"])


def test_runner_rejects_shell_strings() -> None:
    with pytest.raises(TypeError, match="argument list"):
        Runner().run("virsh --version")  # type: ignore[arg-type]


def test_kvm_selects_system_for_managed_nat_even_when_session_queries_succeed() -> None:
    provider = KVMProvider(runner=FakeRunner(), executable_finder=lambda _: "/usr/bin/tool")

    assert provider.connection_uri == "qemu:///system"


def test_kvm_falls_back_to_system_when_a_session_probe_fails() -> None:
    failed = CommandResult(
        ("virsh", "--connect", "qemu:///session", "pool-list", "--all"), 1, "", "denied"
    )
    runner = FakeRunner({failed.argv: failed})
    provider = KVMProvider(runner=runner, executable_finder=lambda _: "/usr/bin/tool")
    assert provider.connection_uri == "qemu:///system"
    assert all(isinstance(command, tuple) for command in runner.commands)


def test_kvm_resource_names_and_ownership_are_deterministic() -> None:
    provider = KVMProvider(runner=FakeRunner())
    owner = Ownership("kvm", "550e8400-e29b-41d4-a716-446655440000", "domain")

    assert provider.resource_name(owner.uid, "node", 2) == (
        "labctl-550e8400-e29b-41d4-a716-446655440000-node-2"
    )
    assert provider.verify_ownership(owner.metadata, owner)
    assert not provider.verify_ownership({**owner.metadata, "labctl.uid": "other"}, owner)


def test_kvm_doctor_checks_prerequisites_and_returns_warnings() -> None:
    failed = CommandResult(
        ("virsh", "--connect", "qemu:///session", "pool-list", "--all"),
        1,
        "",
        "no pool",
    )
    provider = KVMProvider(
        runner=FakeRunner({failed.argv: failed}),
        executable_finder=lambda name: None if name == "virt-install" else f"/usr/bin/{name}",
    )

    health = provider.doctor()

    assert not health.healthy
    assert {check.name for check in health.checks} == {
        "executable:virsh",
        "executable:qemu-img",
        "executable:virt-install",
        "executable:cloud-localds",
        "executable:ssh",
        "platform",
        "connectivity",
        "storage",
        "network",
        "permissions",
        "selected-uri",
        "kvm-device",
        "storage-access",
        "selinux",
    }
    assert any(check.status is HealthStatus.WARNING for check in health.checks)


def test_kvm_doctor_does_not_raise_when_virsh_is_missing() -> None:
    provider = KVMProvider(runner=MissingCommandRunner(), executable_finder=lambda _: None)

    health = provider.doctor()

    assert not health.healthy
    assert len(health.checks) == 14
    assert provider.connection_uri == "qemu:///system"


def test_kvm_doctor_validates_every_libvirt_storage_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance_root = tmp_path / "instances"
    blob_root = tmp_path / "blobs"
    instance_root.mkdir()
    blob_root.mkdir()
    checked: list[Path] = []

    def access(path: os.PathLike[str], mode: int) -> bool:
        candidate = Path(path)
        if mode == os.R_OK | os.W_OK:
            return True
        checked.append(candidate)
        return candidate != blob_root

    monkeypatch.setattr("labctl.kvm.os.access", access)
    provider = KVMProvider(
        runner=FakeRunner(),
        executable_finder=lambda name: f"/usr/bin/{name}",
        storage_paths=(instance_root, blob_root),
    )

    health = provider.doctor()
    storage = next(check for check in health.checks if check.name == "storage-access")

    assert storage.status is HealthStatus.FAIL
    assert str(blob_root) in storage.detail
    assert checked == [instance_root, blob_root]


def test_kvm_doctor_requires_existing_dedicated_storage_and_explains_qemu_access(
    tmp_path: Path,
) -> None:
    root = tmp_path / "missing"
    provider = KVMProvider(
        runner=FakeRunner(),
        executable_finder=lambda name: f"/usr/bin/{name}",
        storage_path=root,
        kvm_path=tmp_path,
    )

    health = provider.doctor()
    storage = next(check for check in health.checks if check.name == "storage-access")

    assert storage.status is HealthStatus.FAIL
    assert str(root) in storage.detail
    assert storage.guidance is not None
    assert "administrator" in storage.guidance
    assert "QEMU DAC" in storage.guidance
    assert "SELinux" in storage.guidance


@pytest.mark.parametrize(
    ("os_release", "expected"),
    [
        ("ID=rhel\nVERSION_ID=9.4\n", HostPlatform("rhel", 9)),
        ("ID=rhel\nVERSION_ID=10\n", HostPlatform("rhel", 10)),
        ("ID=centos\nVERSION_ID=9\n", HostPlatform("centos-stream", 9)),
        ("ID=centos\nVERSION_ID=10\n", HostPlatform("centos-stream", 10)),
        ("ID=rocky\nVERSION_ID=9.5\n", HostPlatform("rocky", 9)),
        ("ID=rocky\nVERSION_ID=10\n", HostPlatform("rocky", 10)),
        ("ID=fedora\nVERSION_ID=44\n", HostPlatform("fedora", 44)),
        ("ID=arch\nBUILD_ID=rolling\n", HostPlatform("arch", None)),
    ],
)
def test_supported_host_platform_matrix(os_release: str, expected: HostPlatform) -> None:
    platform = parse_os_release(os_release)
    guidance = package_guidance(platform)

    assert platform == expected
    assert isinstance(guidance, PackageGuidance)
    assert guidance.install_argv
    assert all(isinstance(part, str) for part in guidance.install_argv)


def test_package_guidance_is_exact_and_unsupported_hosts_fail() -> None:
    assert package_guidance(HostPlatform("rhel", 9)).install_argv == (
        "sudo",
        "dnf",
        "install",
        "qemu-kvm",
        "libvirt",
        "virt-install",
        "libvirt-client",
        "cloud-utils",
    )
    assert package_guidance(HostPlatform("arch", None)).install_argv == (
        "sudo",
        "pacman",
        "-S",
        "qemu-full",
        "libvirt",
        "virt-install",
        "dnsmasq",
        "cloud-image-utils",
    )
    with pytest.raises(ValueError, match="unsupported host platform"):
        package_guidance(HostPlatform("ubuntu", 24))
    with pytest.raises(ValueError, match="unsupported host architecture"):
        package_guidance(HostPlatform("fedora", 44, "aarch64"))
