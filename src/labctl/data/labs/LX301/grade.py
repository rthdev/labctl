#!/usr/bin/python3
# ruff: noqa: E501
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX301"
CHECKS = (
    ("SELinux is enforcing", "/usr/sbin/getenforce", "Enforcing"),
    (
        "SELinux is persistently enforcing",
        "/usr/bin/bash -c 'grep -Eq \"^[[:space:]]*SELINUX=enforcing[[:space:]]*$\" /etc/selinux/config && echo enforcing'",
        "enforcing",
    ),
    (
        "httpd domain is not permissive",
        "/usr/bin/sudo /usr/bin/bash -c '! semanage permissive -l | grep -Fxq httpd_t && echo confined'",
        "confined",
    ),
    ("httpd is enabled", "/usr/bin/systemctl is-enabled httpd", "enabled"),
    ("httpd is active", "/usr/bin/systemctl is-active httpd", "active"),
    (
        "port 8088 has a persistent HTTP label",
        "/usr/bin/sudo /usr/bin/bash -c 'semanage port -l | grep -E "
        '"^http_port_t[[:space:]]+tcp.*(^|[,[:space:]])8088($|[,[:space:]])" >/dev/null && '
        "echo labeled'",
        "labeled",
    ),
    (
        "content has its current and persistent web label",
        "/usr/bin/sudo /usr/bin/bash -c 'actual=$(stat -c %C /srv/secureweb/index.html); "
        "expected=$(matchpathcon -n /srv/secureweb/index.html); "
        'test "$actual" = "$expected" && printf %s "$actual" | grep -q ":httpd_sys_content_t:" && echo labeled\'',
        "labeled",
    ),
    (
        "service responds on port 8088",
        "/usr/bin/curl -fsS http://127.0.0.1:8088/",
        "LX301 secure service",
    ),
)


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("grading context path is required")
        context = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        if context.get("schema_version") != 1 or context.get("lab_id") != LAB_ID:
            raise ValueError("invalid grading context")
        host = context["hosts"]["node"]
        address = host["address"]
        if (
            not isinstance(address, str)
            or not address
            or any(character.isspace() for character in address)
        ):
            raise ValueError("invalid node address")
        ssh = Path(os.environ.get("LABCTL_SSH", "/usr/bin/ssh"))
        identity = Path(host["private_key_path"])
        known_hosts = Path(host["known_hosts_path"])
        if not ssh.is_absolute() or not identity.is_file() or not known_hosts.is_file():
            raise ValueError("absolute SSH and pinned credentials are required")
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {LAB_ID}: {error}", file=sys.stderr)
        return 2

    common = [
        str(ssh),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        str(identity),
        "--",
        f"{host['ssh_user']}@{address}",
    ]
    failed = False
    for description, command, expected in CHECKS:
        result = subprocess.run(  # noqa: S603
            [*common, command], text=True, capture_output=True, check=False
        )
        passed = result.returncode == 0 and result.stdout.strip() == expected
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {description}")
        failed = failed or not passed
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
