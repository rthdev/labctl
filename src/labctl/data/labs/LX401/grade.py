#!/usr/bin/python3
# ruff: noqa: E501
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX401"
CHECKS = (
    (
        "root credential changed and is unlocked",
        "/usr/bin/sudo /usr/bin/bash -c 'hash=$(getent shadow root | cut -d: -f2); "
        'test -n "$hash" && test "$hash" != "$(cat /var/lib/labctl/lx401-root-baseline)" && '
        'case "$hash" in \\!*|\\**) false;; esac && passwd -S root | grep -q " PS " && echo recovered\'',
        "recovered",
    ),
    (
        "host booted after fault staging",
        '/usr/bin/sudo /usr/bin/bash -c \'test "$(cat /proc/sys/kernel/random/boot_id)" != '
        '"$(cat /var/lib/labctl/lx401-boot-baseline)" && echo rebooted\'',
        "rebooted",
    ),
    ("SELinux is enforcing", "/usr/sbin/getenforce", "Enforcing"),
    (
        "SELinux is persistently enforcing",
        "/usr/bin/bash -c 'grep -Eq \"^[[:space:]]*SELINUX=enforcing[[:space:]]*$\" /etc/selinux/config && echo enforcing'",
        "enforcing",
    ),
    (
        "GRUB defaults contain only the corrected audit token",
        '/usr/bin/sudo /usr/bin/bash -c \'grep -Eq "(^|[[:space:]\\"])audit=0([[:space:]\\"]|$)" /etc/default/grub && '
        '! grep -Eq "audit=O|selinux=0|enforcing=0" /etc/default/grub && echo corrected\'',
        "corrected",
    ),
    (
        "every BLS entry contains only the corrected audit token",
        "/usr/bin/sudo /usr/bin/bash -c 'for entry in /boot/loader/entries/*.conf; do "
        'grep -Eq "^options .*([[:space:]])audit=0([[:space:]]|$)" "$entry" && '
        '! grep -Eq "audit=O|selinux=0|enforcing=0" "$entry" || exit 1; done; echo corrected\'',
        "corrected",
    ),
    (
        "running kernel uses only the corrected audit token",
        '/usr/bin/bash -c \'grep -Eq "(^|[[:space:]])audit=0([[:space:]]|$)" /proc/cmdline && '
        '! grep -Eq "audit=O|selinux=0|enforcing=0" /proc/cmdline && echo running-correct\'',
        "running-correct",
    ),
    (
        "fstab validates cleanly",
        "/usr/bin/bash -c 'findmnt --verify --tab-file /etc/fstab >/dev/null && echo valid'",
        "valid",
    ),
    (
        "recovery mount is persistent",
        "/usr/bin/grep -F '/var/lib/labctl-media/lx401-recovery.img /srv/recovery xfs' /etc/fstab",
        "/var/lib/labctl-media/lx401-recovery.img /srv/recovery xfs loop,nofail 0 0",
    ),
    (
        "recovery storage uses the supplied backing image",
        "/usr/bin/sudo /usr/bin/bash -c 'source=$(findmnt -rn -o SOURCE --target /srv/recovery); "
        "image_loop=$(losetup -j /var/lib/labctl-media/lx401-recovery.img | head -n 1 | cut -d: -f1); "
        'test -n "$image_loop" && test "$source" = "$image_loop" && echo mounted\'',
        "mounted",
    ),
    ("system reached normal running state", "/usr/bin/systemctl is-system-running", "running"),
    ("default target is multi-user", "/usr/bin/systemctl get-default", "multi-user.target"),
    (
        "recovery restored the exact SELinux label on shadow",
        "/usr/bin/sudo /usr/bin/bash -c 'actual=$(stat -c %C /etc/shadow); "
        'expected=$(matchpathcon -n /etc/shadow); test "$actual" = "$expected" && '
        'test -z "$(restorecon -n -v /etc/shadow)" && echo labels-clean\'',
        "labels-clean",
    ),
    ("full relabel marker was consumed", "/usr/bin/test ! -e /.autorelabel", ""),
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
