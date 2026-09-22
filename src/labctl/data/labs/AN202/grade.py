#!/usr/bin/python3
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

LAB_ID = "AN202"
TARGETS = ("target",)
STAGING = "/tmp/labctl-an202-grade"  # noqa: S108
KEY = "/home/student/.ssh/id_an202_grade"
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
    host = hosts["target"]
    line = (
        "[storage_web]\n"
        f"target ansible_host={host['address']} ansible_user={host['ssh_user']} "
        f"ansible_ssh_private_key_file={KEY} "
        f"ansible_ssh_common_args='-o StrictHostKeyChecking=yes "
        f"-o UserKnownHostsFile={STAGING}/known_hosts'\n"
    )
    return line.encode("ascii")


def _target_known_hosts(hosts: dict[str, dict[str, str]]) -> bytes:
    return Path(hosts["target"]["known_hosts_path"]).read_bytes()


def _is_sparse_1g(output: str) -> bool:
    try:
        size, blocks = (int(value) for value in output.split())
    except (TypeError, ValueError):
        return False
    return size == 1024**3 and blocks * 512 < size


def _mounted_from_image(findmnt: str, loops: str) -> bool:
    fields = findmnt.split()
    if len(fields) != 2 or fields[1] != "ext4":
        return False
    source = fields[0]
    return any(
        line.split() == [source, "/var/lib/an202/storage.img"] for line in loops.splitlines()
    )


def _persistent_loop_mount(fstab: str) -> bool:
    for raw_line in fstab.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if (
            len(fields) >= 4
            and fields[:3] == ["/var/lib/an202/storage.img", "/srv/an202", "ext4"]
            and "loop" in fields[3].split(",")
        ):
            return True
    return False


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
    target = hosts["target"]
    probe_path: str | None = None
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

        image = _remote(ssh, target, "/usr/bin/stat -c '%s %b' -- /var/lib/an202/storage.img")
        results.append(
            (
                "loop backing file is exactly sparse 1 GiB",
                image.returncode == 0 and _is_sparse_1g(image.stdout),
            )
        )
        mounted = _remote(ssh, target, "/usr/bin/mountpoint -q /srv/an202")
        results.append(("storage is mounted", mounted.returncode == 0))
        findmnt = _remote(ssh, target, "/usr/bin/findmnt -n -o SOURCE,FSTYPE --target /srv/an202")
        loops = _remote(
            ssh, target, "/usr/sbin/losetup --list --noheadings --output NAME,BACK-FILE"
        )
        results.append(
            (
                "mounted ext4 source uses the exact loop backing file",
                findmnt.returncode == 0
                and loops.returncode == 0
                and _mounted_from_image(findmnt.stdout, loops.stdout),
            )
        )
        fstab = _remote(ssh, target, "/usr/bin/cat -- /etc/fstab")
        results.append(
            (
                "loop mount is persistent",
                fstab.returncode == 0 and _persistent_loop_mount(fstab.stdout),
            )
        )
        content = _remote(ssh, target, "/usr/bin/cat -- /srv/an202/www/index.html")
        expected_content = "AN202 durable web"
        results.append(
            (
                "durable mounted content is exact",
                content.returncode == 0 and content.stdout.strip() == expected_content,
            )
        )
        probe_path = f"/srv/an202/www/labctl-an202-probe-{secrets.token_hex(16)}"
        linked = _remote(
            ssh,
            target,
            f"/usr/bin/sudo -n /usr/bin/ln -- /srv/an202/www/index.html {probe_path}",
        )
        served = (
            _remote(
                ssh,
                hosts["controller"],
                f"/usr/bin/curl -fsS --max-time 5 "
                f"http://{target['address']}/{Path(probe_path).name}",
            )
            if linked.returncode == 0
            else None
        )
        results.append(
            (
                "mounted content is served to the controller through the target network",
                content.returncode == 0
                and content.stdout.strip() == expected_content
                and linked.returncode == 0
                and bool(served and served.returncode == 0)
                and bool(served and served.stdout.strip() == expected_content),
            )
        )
        checks = (
            ("httpd is enabled", "/usr/bin/systemctl is-enabled httpd", "enabled"),
            ("httpd is active", "/usr/bin/systemctl is-active httpd", "active"),
            ("firewall allows HTTP", "/usr/bin/firewall-cmd --quiet --query-service=http", None),
            (
                "firewall permanently allows HTTP",
                "/usr/bin/firewall-cmd --quiet --permanent --query-service=http",
                None,
            ),
            ("firewalld is enabled", "/usr/bin/systemctl is-enabled firewalld", "enabled"),
            ("firewalld is active", "/usr/bin/systemctl is-active firewalld", "active"),
            ("SELinux remains enforcing", "/usr/sbin/getenforce", "Enforcing"),
            (
                "web content has its expected SELinux context",
                "/usr/sbin/matchpathcon -V /srv/an202/www/index.html",
                None,
            ),
        )
        for description, command, expected in checks:
            checked = _remote(ssh, target, command)
            passed = checked.returncode == 0 and (
                expected is None or checked.stdout.strip() == expected
            )
            results.append((description, passed))
    finally:
        if probe_path is not None:
            _remote(
                ssh,
                target,
                f"/usr/bin/sudo -n /usr/bin/rm -f -- {probe_path}",
            )
        _remote(ssh, hosts["controller"], CLEANUP_COMMAND)

    for description, passed in results:
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {description}")
    return int(any(not passed for _description, passed in results))


if __name__ == "__main__":
    raise SystemExit(main())
