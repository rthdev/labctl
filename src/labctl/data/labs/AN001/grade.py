#!/usr/bin/python3
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

LAB_ID = "AN001"
STAGING_DIRECTORY = "/tmp/labctl-an001-grade"  # noqa: S108
KEY_COMMAND = (
    "umask 077; /usr/bin/mkdir -p /home/student/.ssh; "
    "test -f /home/student/.ssh/id_an001_grade || "
    "/usr/bin/ssh-keygen -q -t ed25519 -N '' -f /home/student/.ssh/id_an001_grade"
)
PUBLIC_KEY_COMMAND = "/usr/bin/cat -- /home/student/.ssh/id_an001_grade.pub"
PLAYBOOK_COMMAND = (
    "/usr/bin/ansible-playbook -i /tmp/labctl-an001-grade/inventory.ini "
    "/home/student/ansible-lab/site.yml"
)
CLEANUP_COMMAND = "/usr/bin/rm -rf -- /tmp/labctl-an001-grade"
CHECKS = {
    "nginx is enabled": ("/usr/bin/systemctl is-enabled nginx", "enabled"),
    "nginx is active": ("/usr/bin/systemctl is-active nginx", "active"),
    "web content ownership and mode": (
        "/usr/bin/stat -c '%U:%G %a' /usr/share/nginx/html/index.html",
        "root:root 644",
    ),
    "web content is exact": (
        "/usr/bin/base64 -w0 -- /usr/share/nginx/html/index.html",
        base64.b64encode(b"Managed by Ansible").decode("ascii"),
    ),
}
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_PUBLIC_KEY = re.compile(r"^ssh-ed25519 [A-Za-z0-9+/]+={0,3}(?: [A-Za-z0-9@._-]+)?$")


def _host(context: dict[str, Any], name: str) -> dict[str, str]:
    raw = context["hosts"][name]
    if not isinstance(raw, dict):
        raise ValueError(f"{name} host data is invalid")
    required = ("address", "ssh_user", "private_key_path", "known_hosts_path")
    if not all(isinstance(raw.get(field), str) and raw[field] for field in required):
        raise ValueError(f"{name} host data is incomplete")
    address = raw["address"]
    try:
        ipaddress.ip_address(address)
    except ValueError as exc:
        raise ValueError(f"{name} address is invalid") from exc
    if not _USER.fullmatch(raw["ssh_user"]):
        raise ValueError(f"{name} SSH user is invalid")
    identity = Path(raw["private_key_path"])
    known_hosts = Path(raw["known_hosts_path"])
    if not identity.is_absolute() or not identity.is_file():
        raise ValueError(f"{name} identity is invalid")
    if not known_hosts.is_absolute() or not known_hosts.is_file():
        raise ValueError(f"{name} pinned known_hosts is invalid")
    return {field: raw[field] for field in required}


def _ssh_argv(ssh: Path, host: dict[str, str], command: str) -> list[str]:
    return [
        str(ssh),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={host['known_hosts_path']}",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        host["private_key_path"],
        "--",
        f"{host['ssh_user']}@{host['address']}",
        command,
    ]


def _remote(ssh: Path, host: dict[str, str], command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        _ssh_argv(ssh, host, command), text=True, capture_output=True, check=False
    )


def _python_write_command(files: dict[str, bytes]) -> str:
    root = Path(STAGING_DIRECTORY)
    encoded: dict[str, str] = {}
    for path, content in files.items():
        candidate = Path(path)
        if candidate.parent != root or candidate.name in encoded:
            raise ValueError("staged file path is invalid")
        encoded[candidate.name] = base64.b64encode(content).decode("ascii")
    program = f"""import base64, os, shutil, stat
root = {str(root)!r}
try:
    metadata = os.lstat(root)
except FileNotFoundError:
    pass
else:
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        shutil.rmtree(root)
    else:
        os.unlink(root)
os.mkdir(root, 0o700)
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    for name, content in {encoded!r}.items():
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(base64.b64decode(content))
finally:
    os.close(directory)
"""
    return f"/usr/bin/python3 -c {shlex.quote(program)}"


def _install_key_command(public_key: str) -> str:
    encoded = base64.b64encode((public_key + "\n").encode("ascii")).decode("ascii")
    program = (
        "import base64,pathlib; "
        "d=pathlib.Path('/home/student/.ssh'); d.mkdir(mode=0o700,exist_ok=True); "
        "p=d/'authorized_keys'; old=p.read_text() if p.exists() else ''; "
        f"key=base64.b64decode('{encoded}').decode(); "
        "p.write_text(old if key.strip() in old.splitlines() else old+key); p.chmod(0o600)"
    )
    return f"/usr/bin/python3 -c {shlex.quote(program)}"


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("grading context path must be the first argument")
        context = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        if context.get("schema_version") != 1 or context.get("lab_id") != LAB_ID:
            raise ValueError("grading context does not describe AN001 schema v1")
        controller = _host(context, "controller")
        target = _host(context, "target")
        ssh = Path(os.environ.get("LABCTL_SSH", "/usr/bin/ssh"))
        if not ssh.is_absolute() or not ssh.is_file():
            raise ValueError("absolute SSH executable is required")
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {LAB_ID}: {error}", file=sys.stderr)
        return 2

    results: list[tuple[str, bool]] = []
    arranged = False
    try:
        generated = _remote(ssh, controller, KEY_COMMAND)
        obtained = (
            _remote(ssh, controller, PUBLIC_KEY_COMMAND) if generated.returncode == 0 else None
        )
        public_key = obtained.stdout.strip() if obtained is not None else ""
        valid_key = bool(
            obtained and obtained.returncode == 0 and _PUBLIC_KEY.fullmatch(public_key)
        )
        installed = _remote(ssh, target, _install_key_command(public_key)) if valid_key else None
        inventory = (
            "[web]\n"
            f"target ansible_host={target['address']} ansible_user={target['ssh_user']} "
            "ansible_ssh_private_key_file=/home/student/.ssh/id_an001_grade "
            "ansible_ssh_common_args='-o StrictHostKeyChecking=yes "
            "-o UserKnownHostsFile=/tmp/labctl-an001-grade/known_hosts'\n"
        ).encode("ascii")
        known_hosts = Path(target["known_hosts_path"]).read_bytes()
        transferred = (
            _remote(
                ssh,
                controller,
                _python_write_command(
                    {
                        "/tmp/labctl-an001-grade/inventory.ini": inventory,  # noqa: S108
                        "/tmp/labctl-an001-grade/known_hosts": known_hosts,  # noqa: S108
                    }
                ),
            )
            if installed is not None and installed.returncode == 0
            else None
        )
        arranged = bool(transferred is not None and transferred.returncode == 0)
        results.append(("dedicated controller-to-target SSH is arranged", arranged))
        played = _remote(ssh, controller, PLAYBOOK_COMMAND) if arranged else None
        results.append(
            ("controller playbook completed successfully", bool(played and played.returncode == 0))
        )
    finally:
        _remote(ssh, controller, CLEANUP_COMMAND)

    for description, (command, expected) in CHECKS.items():
        checked = _remote(ssh, target, command)
        results.append(
            (description, checked.returncode == 0 and checked.stdout.strip() == expected)
        )

    for description, passed in results:
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {description}")
    return 1 if any(not passed for _description, passed in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
