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

LAB_ID = "AN402"
MANAGED = ("web1", "web2", "web3")
STAGING_DIRECTORY = "/tmp/labctl-an402-grade"  # noqa: S108
KEY_PATH = "/home/student/.ssh/id_an402_grade"
KEY_COMMAND = (
    "umask 077; /usr/bin/mkdir -p /home/student/.ssh; "
    f"test -f {KEY_PATH} || /usr/bin/ssh-keygen -q -t ed25519 -N '' -f {KEY_PATH}"
)
PUBLIC_KEY_COMMAND = f"/usr/bin/cat -- {KEY_PATH}.pub"
PLAYBOOK_COMMAND = (
    f"/usr/bin/env ANSIBLE_CONFIG={STAGING_DIRECTORY}/ansible.cfg "
    "ANSIBLE_STDOUT_CALLBACK=default "
    f"ANSIBLE_CALLBACK_PLUGINS={STAGING_DIRECTORY}/callback_plugins "
    "ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0 /usr/bin/ansible-playbook "
    f"-i {STAGING_DIRECTORY}/inventory.ini "
    "/home/student/ansible-lab/site.yml"
)
SUMMARY_COMMAND = "/usr/bin/cat -- /home/student/ansible-lab/recovery-summary.json"
RESET_SUMMARY_COMMAND = "/usr/bin/rm -f -- /home/student/ansible-lab/recovery-summary.json"
CLEANUP_COMMAND = f"/usr/bin/rm -rf -- {STAGING_DIRECTORY}"
EXPECTED_CONFIG = base64.b64encode(
    b"ProxyPass /api/ http://127.0.0.1:8080/\nProxyPassReverse /api/ http://127.0.0.1:8080/\n"
).decode("ascii")
CHECKS = {
    "web service is enabled": ("/usr/bin/systemctl is-enabled httpd", "enabled"),
    "web service is active": ("/usr/bin/systemctl is-active httpd", "active"),
    "backend service is enabled": (
        "/usr/bin/systemctl is-enabled an402-backend",
        "enabled",
    ),
    "backend service is active": ("/usr/bin/systemctl is-active an402-backend", "active"),
    "proxy configuration is correct": (
        "/usr/bin/sudo -n /usr/bin/base64 -w0 -- /etc/httpd/conf.d/an402.conf",
        EXPECTED_CONFIG,
    ),
    "firewall service is enabled": ("/usr/bin/systemctl is-enabled firewalld", "enabled"),
    "runtime firewall allows HTTP": (
        "/usr/bin/firewall-cmd --quiet --query-service=http",
        "",
    ),
    "permanent firewall allows HTTP": (
        "/usr/bin/firewall-cmd --quiet --permanent --query-service=http",
        "",
    ),
    "SELinux is enforcing": ("/usr/sbin/getenforce", "Enforcing"),
    "SELinux permits backend connections": (
        "/usr/sbin/getsebool httpd_can_network_connect",
        "httpd_can_network_connect --> on",
    ),
    "backend is healthy through the proxy": (
        "/usr/bin/curl -fsS http://127.0.0.1/api/health",
        "ok",
    ),
    "platform landing page is correct": (
        "/usr/bin/curl -fsS http://127.0.0.1/",
        "AN402 platform ready",
    ),
}
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_PUBLIC_KEY = re.compile(r"^ssh-ed25519 [A-Za-z0-9+/]+={0,3}(?: [A-Za-z0-9@._-]+)?$")
_RECAP = re.compile(
    r"^(?P<host>[^\s:]+)\s*:\s*.*?changed=(?P<changed>\d+)\s+"
    r"unreachable=(?P<unreachable>\d+)\s+failed=(?P<failed>\d+)\b",
    re.MULTILINE,
)


def _host(context: dict[str, Any], name: str) -> dict[str, str]:
    raw = context["hosts"][name]
    if not isinstance(raw, dict):
        raise ValueError(f"{name} host data is invalid")
    required = ("address", "ssh_user", "private_key_path", "known_hosts_path")
    if not all(isinstance(raw.get(field), str) and raw[field] for field in required):
        raise ValueError(f"{name} host data is incomplete")
    try:
        ipaddress.ip_address(raw["address"])
    except ValueError as exc:
        raise ValueError(f"{name} address is invalid") from exc
    if not _USER.fullmatch(raw["ssh_user"]):
        raise ValueError(f"{name} SSH user is invalid")
    for field in ("private_key_path", "known_hosts_path"):
        path = Path(raw[field])
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f"{name} {field} is invalid")
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


def _idempotent_recap(output: str) -> bool:
    rows = {
        match.group("host"): tuple(
            int(match.group(field)) for field in ("changed", "unreachable", "failed")
        )
        for match in _RECAP.finditer(output)
    }
    return set(rows) == set(MANAGED) and all(values == (0, 0, 0) for values in rows.values())


def _valid_summary(output: str) -> bool:
    try:
        summary = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(summary, dict):
        return False
    if summary.get("lab") != LAB_ID or summary.get("status") != "recovered":
        return False
    hosts = summary.get("hosts")
    if not isinstance(hosts, list) or not all(isinstance(item, dict) for item in hosts):
        return False
    entries = [(item.get("name"), item.get("status")) for item in hosts]
    return len(entries) == len(MANAGED) and set(entries) == {(name, "healthy") for name in MANAGED}


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("grading context path must be the first argument")
        context = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        if context.get("schema_version") != 1 or context.get("lab_id") != LAB_ID:
            raise ValueError(f"grading context does not describe {LAB_ID} schema v1")
        controller = _host(context, "controller")
        targets = {name: _host(context, name) for name in MANAGED}
        ssh = Path(os.environ.get("LABCTL_SSH", "/usr/bin/ssh"))
        if not ssh.is_absolute() or not ssh.is_file():
            raise ValueError("absolute SSH executable is required")
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {LAB_ID}: {error}", file=sys.stderr)
        return 2

    results: list[tuple[str, bool]] = []
    try:
        generated = _remote(ssh, controller, KEY_COMMAND)
        obtained = (
            _remote(ssh, controller, PUBLIC_KEY_COMMAND) if generated.returncode == 0 else None
        )
        public_key = obtained.stdout.strip() if obtained is not None else ""
        valid_key = bool(
            obtained and obtained.returncode == 0 and _PUBLIC_KEY.fullmatch(public_key)
        )
        installed = (
            {
                name: _remote(ssh, target, _install_key_command(public_key))
                for name, target in targets.items()
            }
            if valid_key
            else {}
        )
        inventory_lines = ["[platform]"]
        for name, target in targets.items():
            inventory_lines.append(
                f"{name} ansible_host={target['address']} ansible_user={target['ssh_user']} "
                f"ansible_ssh_private_key_file={KEY_PATH} "
                f"ansible_ssh_common_args='-o StrictHostKeyChecking=yes "
                f"-o UserKnownHostsFile={STAGING_DIRECTORY}/known_hosts'"
            )
        transferred = None
        if len(installed) == len(MANAGED) and all(
            result.returncode == 0 for result in installed.values()
        ):
            transferred = _remote(
                ssh,
                controller,
                _python_write_command(
                    {
                        f"{STAGING_DIRECTORY}/inventory.ini": (
                            "\n".join(inventory_lines) + "\n"
                        ).encode("ascii"),
                        f"{STAGING_DIRECTORY}/ansible.cfg": (
                            b"[defaults]\nstdout_callback = default\n"
                        ),
                        f"{STAGING_DIRECTORY}/known_hosts": b"".join(
                            Path(targets[name]["known_hosts_path"]).read_bytes() for name in MANAGED
                        ),
                    }
                ),
            )
        arranged = bool(transferred is not None and transferred.returncode == 0)
        results.append(("dedicated controller-to-target SSH is arranged", arranged))
        reset_summary = _remote(ssh, controller, RESET_SUMMARY_COMMAND) if arranged else None
        summary_reset = bool(reset_summary and reset_summary.returncode == 0)
        results.append(("prior controller recovery summary is invalidated", summary_reset))
        first = _remote(ssh, controller, PLAYBOOK_COMMAND) if summary_reset else None
        first_ok = bool(first and first.returncode == 0)
        results.append(("first controller playbook run completed successfully", first_ok))
        second = _remote(ssh, controller, PLAYBOOK_COMMAND) if first_ok else None
        second_ok = bool(second and second.returncode == 0)
        results.append(("second controller playbook run completed successfully", second_ok))
        results.append(
            (
                "second playbook run reports changed=0 and failures=0 for all targets",
                bool(second_ok and second and _idempotent_recap(second.stdout)),
            )
        )
        summary = _remote(ssh, controller, SUMMARY_COMMAND) if second_ok else None
        results.append(
            (
                "controller recovery summary has the required schema and healthy hosts",
                bool(summary and summary.returncode == 0 and _valid_summary(summary.stdout)),
            )
        )
    finally:
        _remote(ssh, controller, CLEANUP_COMMAND)

    for name, target in targets.items():
        for description, (command, expected) in CHECKS.items():
            checked = _remote(ssh, target, command)
            results.append(
                (
                    f"{name} {description}",
                    checked.returncode == 0 and checked.stdout.strip() == expected,
                )
            )

    for description, passed in results:
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {description}")
    return 1 if any(not passed for _description, passed in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
