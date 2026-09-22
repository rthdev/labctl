"""SSH key and strict host-verification command helpers."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

KeygenRunner = Callable[..., object]


def generate_keypair(
    directory: Path,
    *,
    name: str = "id_lab",
    runner: KeygenRunner = subprocess.run,
) -> tuple[Path, Path]:
    """Generate a dedicated passwordless Ed25519 keypair without a shell."""
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ValueError("key name must be a plain filename")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    private = directory / name
    public = directory / f"{name}.pub"
    runner(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
        check=True,
    )
    os.chmod(private, 0o600)
    os.chmod(public, 0o644)
    return private, public


def write_known_host(host: str, host_key: str, known_hosts: Path) -> None:
    """Write the provisioned host key to an isolated known_hosts file."""
    if not host or any(character.isspace() for character in host):
        raise ValueError("invalid host")
    if "\n" in host_key or not host_key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-")):
        raise ValueError("invalid host key")
    known_hosts.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    known_hosts.write_text(f"{host} {host_key}\n", encoding="utf-8")
    os.chmod(known_hosts, 0o600)


def build_ssh_command(
    user: str,
    host: str,
    identity_file: Path,
    known_hosts: Path,
    remote_command: tuple[str, ...] = (),
) -> list[str]:
    """Build argv that cannot silently trust a changed or unknown host key."""
    if not user or not host or any(character.isspace() for character in user + host):
        raise ValueError("invalid SSH destination")
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        str(identity_file),
        "--",
        f"{user}@{host}",
        *remote_command,
    ]
