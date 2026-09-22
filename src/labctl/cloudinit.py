"""Secure cloud-init rendering and phased readiness polling."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

import yaml

from .proxy import configure_proxy


def generate_cloud_init(public_key: str, setup_commands: Sequence[str]) -> str:
    """Render cloud-init that permits key authentication and runs setup once."""
    if not public_key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
        raise ValueError("unsupported SSH public key")
    if "\n" in public_key:
        raise ValueError("SSH public key must be one line")
    script_lines = [
        "#!/bin/sh",
        "set -eu",
        "test -e /var/lib/labctl/setup.done && exit 0",
        "mkdir -p /var/lib/labctl",
        *setup_commands,
        "touch /var/lib/labctl/setup.done",
    ]
    document = {
        "ssh_pwauth": False,
        "disable_root": True,
        "users": [
            {
                "name": "student",
                "lock_passwd": True,
                "ssh_authorized_keys": [public_key],
            }
        ],
        "write_files": [
            {
                "path": "/usr/local/sbin/labctl-setup",
                "permissions": "0700",
                "owner": "root:root",
                "content": "\n".join(script_lines) + "\n",
            }
        ],
        "runcmd": [["/usr/local/sbin/labctl-setup"]],
    }
    configure_proxy(document)
    rendered: str = yaml.safe_dump(document, sort_keys=False)
    return "#cloud-config\n" + rendered


@dataclass(frozen=True)
class ReadinessPhase:
    name: str
    timeout: float
    probe: Callable[[], bool]


class ReadinessTimeout(TimeoutError):
    """A named readiness phase exceeded its own deadline."""


def wait_ready(
    phases: Iterable[ReadinessPhase],
    *,
    interval: float = 1.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait for address, transport, and guest readiness in distinct phases."""
    if interval <= 0:
        raise ValueError("interval must be positive")
    for phase in phases:
        if phase.timeout < 0:
            raise ValueError(f"timeout for {phase.name} must be non-negative")
        deadline = monotonic() + phase.timeout
        while not phase.probe():
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ReadinessTimeout(f"readiness phase timed out: {phase.name}")
            sleep(min(interval, remaining))
