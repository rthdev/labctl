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

LAB_ID = "AN201"
TARGETS = ("app", "proxy")
STAGING = "/tmp/labctl-an201-grade"  # noqa: S108
KEY = "/home/student/.ssh/id_an201_grade"
KEY_COMMAND = (
    "umask 077; /usr/bin/mkdir -p /home/student/.ssh; "
    f"test -f {KEY} || /usr/bin/ssh-keygen -q -t ed25519 -N '' -f {KEY}"
)
PUBLIC_KEY_COMMAND = f"/usr/bin/cat -- {KEY}.pub"
PLAYBOOK_COMMAND = (
    f"/usr/bin/ansible-playbook -i {STAGING}/inventory.ini /home/student/ansible-lab/site.yml"
)
CLEANUP_COMMAND = f"/usr/bin/rm -rf -- {STAGING}"
_PUBLIC_KEY = re.compile(r"^ssh-ed25519 [A-Za-z0-9+/]+={0,3}(?: [A-Za-z0-9@._-]+)?$")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _host(context: dict[str, Any], name: str) -> dict[str, str]:
    raw = context["hosts"][name]
    required = ("address", "ssh_user", "private_key_path", "known_hosts_path")
    if not isinstance(raw, dict) or not all(
        isinstance(raw.get(k), str) and raw[k] for k in required
    ):
        raise ValueError(f"{name} host data is incomplete")
    ipaddress.ip_address(raw["address"])
    if not _USER.fullmatch(raw["ssh_user"]):
        raise ValueError(f"{name} SSH user is invalid")
    for field in ("private_key_path", "known_hosts_path"):
        path = Path(raw[field])
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f"{name} {field} is invalid")
    return {field: raw[field] for field in required}


def _remote(ssh: Path, host: dict[str, str], command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [
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
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def _install_key(public_key: str) -> str:
    encoded = base64.b64encode((public_key + "\n").encode("ascii")).decode("ascii")
    program = (
        "import base64,pathlib; d=pathlib.Path('/home/student/.ssh'); "
        "d.mkdir(mode=0o700,exist_ok=True); p=d/'authorized_keys'; "
        "old=p.read_text() if p.exists() else ''; "
        f"key=base64.b64decode('{encoded}').decode(); "
        "p.write_text(old if key.strip() in old.splitlines() else old+key); p.chmod(0o600)"
    )
    return f"/usr/bin/python3 -c {shlex.quote(program)}"


def _stage(files: dict[str, bytes]) -> str:
    encoded = {name: base64.b64encode(content).decode("ascii") for name, content in files.items()}
    program = f"""import base64, os, shutil, stat
root={STAGING!r}
try:
    metadata=os.lstat(root)
except FileNotFoundError:
    pass
else:
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode): shutil.rmtree(root)
    else: os.unlink(root)
os.mkdir(root, 0o700)
directory=os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    for name, content in {encoded!r}.items():
        descriptor=os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory)
        with os.fdopen(descriptor, 'wb') as stream: stream.write(base64.b64decode(content))
finally:
    os.close(directory)
"""
    return f"/usr/bin/python3 -c {shlex.quote(program)}"


def _inventory(hosts: dict[str, dict[str, str]]) -> bytes:
    lines: list[str] = []
    for group, names in (("app_servers", ("app",)), ("proxies", ("proxy",))):
        lines.append(f"[{group}]")
        for name in names:
            host = hosts[name]
            lines.append(
                f"{name} ansible_host={host['address']} ansible_user={host['ssh_user']} "
                f"ansible_ssh_private_key_file={KEY} "
                f"ansible_ssh_common_args='-o StrictHostKeyChecking=yes "
                f"-o UserKnownHostsFile={STAGING}/known_hosts'"
            )
    return ("\n".join(lines) + "\n").encode("ascii")


def _target_known_hosts(hosts: dict[str, dict[str, str]]) -> bytes:
    entries = [Path(hosts[name]["known_hosts_path"]).read_bytes().rstrip(b"\n") for name in TARGETS]
    return b"\n".join(entries) + b"\n"


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("grading context path must be the first argument")
        context = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        if context.get("schema_version") != 1 or context.get("lab_id") != LAB_ID:
            raise ValueError("grading context is invalid")
        hosts = {name: _host(context, name) for name in (*TARGETS, "controller")}
        ssh = Path(os.environ.get("LABCTL_SSH", "/usr/bin/ssh"))
        if not ssh.is_absolute() or not ssh.is_file():
            raise ValueError("absolute SSH executable is required")
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {LAB_ID}: {error}", file=sys.stderr)
        return 2

    results: list[tuple[str, bool]] = []
    try:
        generated = _remote(ssh, hosts["controller"], KEY_COMMAND)
        obtained = (
            _remote(ssh, hosts["controller"], PUBLIC_KEY_COMMAND)
            if generated.returncode == 0
            else None
        )
        public_key = obtained.stdout.strip() if obtained else ""
        valid = bool(obtained and obtained.returncode == 0 and _PUBLIC_KEY.fullmatch(public_key))
        installed = valid and all(
            _remote(ssh, hosts[name], _install_key(public_key)).returncode == 0 for name in TARGETS
        )
        staged = False
        if installed:
            staged = (
                _remote(
                    ssh,
                    hosts["controller"],
                    _stage(
                        {
                            "inventory.ini": _inventory(hosts),
                            "known_hosts": _target_known_hosts(hosts),
                        }
                    ),
                ).returncode
                == 0
            )
        results.append(("dedicated controller SSH reaches every target", bool(staged)))
        played = _remote(ssh, hosts["controller"], PLAYBOOK_COMMAND) if staged else None
        results.append(
            ("controller playbook completed successfully", bool(played and played.returncode == 0))
        )

        for name in TARGETS:
            enabled = _remote(ssh, hosts[name], "/usr/bin/systemctl is-enabled nginx")
            active = _remote(ssh, hosts[name], "/usr/bin/systemctl is-active nginx")
            results.append(
                (
                    f"{name} nginx is enabled",
                    enabled.returncode == 0 and enabled.stdout.strip() == "enabled",
                )
            )
            results.append(
                (
                    f"{name} nginx is active",
                    active.returncode == 0 and active.stdout.strip() == "active",
                )
            )
        app_response = _remote(
            ssh, hosts["app"], "/usr/bin/curl -fsS --max-time 5 http://127.0.0.1/"
        )
        results.append(
            (
                "app serves the application directly",
                app_response.returncode == 0 and app_response.stdout.strip() == "AN201 application",
            )
        )
        response = _remote(ssh, hosts["proxy"], "/usr/bin/curl -fsS --max-time 5 http://127.0.0.1/")
        results.append(
            (
                "proxy serves the application end to end",
                response.returncode == 0 and response.stdout.strip() == "AN201 application",
            )
        )
        stopped = _remote(ssh, hosts["app"], "/usr/bin/sudo -n /usr/bin/systemctl stop nginx")
        probe = None
        restarted = None
        if stopped.returncode == 0:
            try:
                probe = _remote(
                    ssh,
                    hosts["proxy"],
                    "/usr/bin/curl -fsS --max-time 5 -H 'X-Labctl-Probe: backend-stopped' "
                    "http://127.0.0.1/",
                )
            finally:
                restarted = _remote(
                    ssh, hosts["app"], "/usr/bin/sudo -n /usr/bin/systemctl start nginx"
                )
        results.append(
            (
                "proxy depends on the application backend",
                bool(
                    stopped.returncode == 0
                    and probe
                    and probe.returncode != 0
                    and restarted
                    and restarted.returncode == 0
                ),
            )
        )
    finally:
        _remote(ssh, hosts["controller"], CLEANUP_COMMAND)

    for description, passed in results:
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {description}")
    return int(any(not passed for _description, passed in results))


if __name__ == "__main__":
    raise SystemExit(main())
