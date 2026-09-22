"""Supported host platform detection and exact virtualization package guidance."""

from __future__ import annotations

import platform as platform_module
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class HostPlatform:
    distribution: str
    major_version: int | None
    architecture: str = "x86_64"


@dataclass(frozen=True, slots=True)
class PackageGuidance:
    install_argv: tuple[str, ...]

    @property
    def command(self) -> str:
        return shlex.join(self.install_argv)


_RPM_PACKAGES = ("qemu-kvm", "libvirt", "virt-install", "libvirt-client", "cloud-utils")
_ARCH_PACKAGES = ("qemu-full", "libvirt", "virt-install", "dnsmasq", "cloud-image-utils")
_SUPPORTED = {
    HostPlatform(distribution, version)
    for distribution in ("rhel", "centos-stream", "rocky")
    for version in (9, 10)
} | {HostPlatform("fedora", 44), HostPlatform("arch", None)}


def parse_os_release(content: str) -> HostPlatform:
    """Parse the relevant fields from os-release content."""

    values: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip("\"'")
    distribution = values.get("ID", "").lower()
    if distribution == "centos":
        distribution = "centos-stream"
    version_text = values.get("VERSION_ID", "").split(".", 1)[0]
    major = int(version_text) if version_text.isdigit() else None
    return HostPlatform(distribution, major)


def detect_host(os_release: str | None = None, *, architecture: str | None = None) -> HostPlatform:
    """Detect OS release and architecture without changing the host."""
    content = (
        os_release
        if os_release is not None
        else Path("/etc/os-release").read_text(encoding="utf-8")
    )
    detected = parse_os_release(content)
    return HostPlatform(
        detected.distribution,
        detected.major_version,
        architecture or platform_module.machine(),
    )


def package_guidance(platform: HostPlatform) -> PackageGuidance:
    """Return an argv-safe install command for an explicitly supported host."""

    if platform.architecture != "x86_64":
        raise ValueError(
            f"unsupported host architecture: {platform.architecture}; supported: x86_64"
        )
    if HostPlatform(platform.distribution, platform.major_version) not in _SUPPORTED:
        version = "rolling" if platform.major_version is None else str(platform.major_version)
        raise ValueError(
            f"unsupported host platform: {platform.distribution} {version}; supported: "
            "RHEL, CentOS Stream, and Rocky 9/10; Fedora 44; Arch"
        )
    if platform.distribution == "arch":
        return PackageGuidance(("sudo", "pacman", "-S", *_ARCH_PACKAGES))
    return PackageGuidance(("sudo", "dnf", "install", *_RPM_PACKAGES))
