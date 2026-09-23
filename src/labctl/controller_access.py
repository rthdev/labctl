"""Opt-in, guest-generated practice credentials; never export private keys."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
from typing import Any

from labctl.definitions import ControllerAccessDefinition

PRACTICE = "/home/student/.ssh/labctl-practice"


def public_key(value: str) -> str:
    """Accept only a complete Ed25519 wire key, dropping any printable comment."""
    if not isinstance(value, str) or not re.fullmatch(
        r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}(?: [ -~]+)?", value
    ):
        raise ValueError("invalid practice public key")
    encoded = value.split()[1]
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ValueError("invalid practice public key") from exc
    if len(decoded) != 51 or decoded[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
        raise ValueError("invalid practice public key")
    return f"ssh-ed25519 {encoded}"


# This code runs as student, never root. Every filesystem operation is anchored
# to open directory descriptors; neither symlinks nor hard-linked files are used.
_GUEST = r"""
import os
import stat
import subprocess
import uuid

os.umask(0o077)

def directory(parent, name, create=False):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        os.close(fd)
        raise ValueError('unsafe practice directory')
    return fd

# Walk even the home ancestors without following links. Root-owned /home is
# allowed, but the final student home and every managed child must belong to us.
fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
for part in '/home/student'.strip('/').split('/'):
    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    os.close(fd)
    fd = child
home = fd
info = os.fstat(home)
if info.st_uid != os.getuid() or info.st_mode & 0o022:
    raise ValueError('unsafe practice home')
ssh = directory(home, '.ssh', True)

def read(parent, name):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_mode & 0o022
                or info.st_size > 1048576):
            raise ValueError('unsafe practice file')
        return os.read(fd, 1048577).decode('utf-8')
    finally:
        os.close(fd)


def write(parent, name, value):
    read(parent, name)
    temporary = '.labctl-' + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
        if read(parent, name) != value:
            raise ValueError('practice file verification failed')
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
"""


def key_script() -> str:
    """Generate a dedicated Ed25519 key inside the controller, return only public."""
    return (
        _GUEST
        + r"""
keys = directory(ssh, 'labctl-practice', True)
private = read(keys, 'id_ed25519')
read(keys, 'id_ed25519.pub')
if private is None:
    temporary = '.key-' + uuid.uuid4().hex
    os.mkdir(temporary, 0o700, dir_fd=keys)
    staging = directory(keys, temporary)
    try:
        path = '/proc/self/fd/' + str(staging) + '/key'
        subprocess.run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', path],
                       pass_fds=(staging,), check=True, capture_output=True, timeout=20)
        for source, target in [('key', 'id_ed25519'), ('key.pub', 'id_ed25519.pub')]:
            os.rename(source, target, src_dir_fd=staging, dst_dir_fd=keys)
        os.fsync(keys)
    finally:
        for name in ('key', 'key.pub'):
            try:
                os.unlink(name, dir_fd=staging)
            except FileNotFoundError:
                pass
        os.close(staging)
        os.rmdir(temporary, dir_fd=keys)
fd = os.open('id_ed25519', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=keys)
try:
    info = os.fstat(fd)
    if info.st_mode & 0o077 or info.st_nlink != 1 or info.st_uid != os.getuid():
        raise ValueError('unsafe practice private key')
    path = '/proc/self/fd/' + str(fd)
    result = subprocess.run(['/usr/bin/ssh-keygen', '-y', '-P', '', '-f', path],
                            pass_fds=(fd,), check=True, capture_output=True, text=True, timeout=20)
    print(' '.join(result.stdout.split()[:2]))
finally:
    os.close(fd)
"""
    )


def authorize_script(key: str) -> str:
    """Replace only the managed key block, preserving management/learner keys."""
    key = public_key(key)
    return (
        _GUEST
        + f"\nkey = {key!r}\n"
        + r"""
existing = read(ssh, 'authorized_keys') or ''
begin = '# BEGIN LABCTL PRACTICE KEY\n'
end = '# END LABCTL PRACTICE KEY\n'
if begin in existing or end in existing:
    if (existing.count(begin) != 1 or existing.count(end) != 1
            or existing.index(end) < existing.index(begin)):
        raise ValueError('invalid managed authorized key block')
    before, rest = existing.split(begin, 1)
    _, after = rest.split(end, 1)
    existing = before + after
write(ssh, 'authorized_keys', existing.rstrip('\n') + '\n' + begin + key + '\n' + end)
"""
    )


def controller_script(access: ControllerAccessDefinition, hosts: dict[str, dict[str, Any]]) -> str:
    """Render managed SSH aliases and practice inventory from pinned state."""
    config: list[str] = []
    known: list[str] = []
    groups: dict[str, list[str]] = {}
    for name, memberships in access.targets:
        host = hosts[name]
        if not isinstance(host["address"], str) or "%" in host["address"]:
            raise ValueError("invalid practice host address")
        address = str(ipaddress.ip_address(host["address"]))
        key = public_key(host["host_public_key"])
        known.append(f"{address} {key}")
        config.extend(
            [
                f"Host {name} {address}",
                f"  HostName {address}",
                "  User student",
                f"  IdentityFile {PRACTICE}/id_ed25519",
                "  IdentitiesOnly yes",
                "  IdentityAgent none",
                "  BatchMode yes",
                "  StrictHostKeyChecking yes",
                f"  UserKnownHostsFile {PRACTICE}/known_hosts",
                f"  GlobalKnownHostsFile {PRACTICE}/known_hosts",
            ]
        )
        for group in memberships:
            groups.setdefault(group, []).append(
                f"{name} ansible_host={address} ansible_user=student "
                f"ansible_ssh_private_key_file={PRACTICE}/id_ed25519 "
                f"ansible_ssh_common_args='-F {PRACTICE}/config'"
            )
    config.append("Host *")
    inventory = "\n".join(
        line for group, entries in groups.items() for line in [f"[{group}]", *entries, ""]
    )
    payload = {
        "config": "\n".join(config) + "\n",
        "known_hosts": "\n".join(known) + "\n",
        "inventory.ini": inventory,
    }
    return (
        _GUEST
        + f"\npayload = {payload!r}\n"
        + r"""
keys = directory(ssh, 'labctl-practice', True)
project = directory(home, 'ansible-lab', True)
begin = '# BEGIN LABCTL PRACTICE\n'
end = '# END LABCTL PRACTICE\n'
existing = read(ssh, 'config') or ''
if begin in existing or end in existing:
    if (existing.count(begin) != 1 or existing.count(end) != 1
            or not existing.startswith(begin) or existing.index(end) < len(begin)):
        raise ValueError('invalid managed SSH config block')
    existing = existing.split(end, 1)[1]
managed = (begin + 'Include /home/student/.ssh/labctl-practice/config\n'
           + 'Host *\n' + end)
# Validate every destination before publishing any content.
for name in ('config', 'known_hosts'):
    read(keys, name)
read(project, 'inventory.ini')
write(keys, 'known_hosts', payload['known_hosts'])
write(keys, 'config', payload['config'])
write(project, 'inventory.ini', payload['inventory.ini'])
write(ssh, 'config', managed + existing)
"""
    )
