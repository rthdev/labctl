#!/usr/bin/python3
# ruff: noqa: E501
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX302"
CHECKS = (
    (
        "volume group uses both supplied backing images",
        "/usr/bin/sudo /usr/bin/bash -c 'pv1=$(losetup -j /var/lib/labctl-media/lx302-pv1.img | head -n 1 | cut -d: -f1); "
        "pv2=$(losetup -j /var/lib/labctl-media/lx302-pv2.img | head -n 1 | cut -d: -f1); "
        'test -n "$pv1" && test -n "$pv2" && test "$pv1" != "$pv2" && '
        'test "$(pvs --noheadings -o vg_name "$pv1" | tr -d " ")" = vgdata && '
        'test "$(pvs --noheadings -o vg_name "$pv2" | tr -d " ")" = vgdata && echo supplied\'',
        "supplied",
    ),
    (
        "volume group contains both physical volumes",
        "/usr/bin/sudo /usr/bin/bash -c 'test \"$(vgs --noheadings -o pv_count --nosuffix "
        'vgdata | tr -d " ")" -ge 2 && echo expanded\'',
        "expanded",
    ),
    (
        "logical volume reached the target size",
        "/usr/bin/sudo /usr/bin/bash -c 'test \"$(lvs --noheadings --units m --nosuffix -o "
        'lv_size vgdata/lvdata | cut -d. -f1 | tr -d " ")" -ge 700 && echo large-enough\'',
        "large-enough",
    ),
    (
        "host rebooted after durable storage configuration",
        '/usr/bin/sudo /usr/bin/bash -c \'test "$(cat /proc/sys/kernel/random/boot_id)" != '
        '"$(cat /var/lib/labctl/lx302-boot-baseline)" && echo rebooted\'',
        "rebooted",
    ),
    (
        "loop PV attachment is enabled for boot",
        "/usr/bin/systemctl is-enabled lx302-loop-pvs.service",
        "enabled",
    ),
    (
        "loop PV attachment survived boot",
        "/usr/bin/systemctl is-active lx302-loop-pvs.service",
        "active",
    ),
    (
        "boot attachment runs before local filesystems and LVM",
        "/usr/bin/sudo /usr/bin/bash -c 'unit=$(systemctl cat lx302-loop-pvs.service); "
        'printf %s "$unit" | grep -Eq "^DefaultDependencies=no$" && '
        'printf %s "$unit" | grep -Eq "^Before=.*local-fs-pre.target" && '
        'printf %s "$unit" | grep -Eq "^Before=.*lvm2-monitor.service" && '
        'printf %s "$unit" | grep -Eq "^ExecStart=.*/losetup .*lx302-pv1.img" && '
        'printf %s "$unit" | grep -Eq "^ExecStart=.*/losetup .*lx302-pv2.img" && '
        'printf %s "$unit" | grep -Eq "^ExecStart=.*/pvscan([[:space:]]|$)" && '
        'printf %s "$unit" | grep -Eq "^WantedBy=sysinit.target$" && '
        "systemd-analyze verify lx302-loop-pvs.service >/dev/null 2>&1 && echo ordered'",
        "ordered",
    ),
    (
        "fstab validates cleanly",
        "/usr/bin/bash -c 'findmnt --verify --tab-file /etc/fstab >/dev/null && echo valid'",
        "valid",
    ),
    (
        "logical volume mount is persistent",
        "/usr/bin/bash -c 'grep -Eq \"^[^#]*(/dev/vgdata/lvdata|/dev/mapper/vgdata-lvdata)[[:space:]]+/srv/lvmdata[[:space:]]+xfs([[:space:]]|$)\" /etc/fstab && echo persistent'",
        "persistent",
    ),
    (
        "exact recovered filesystem is mounted after reboot",
        "/usr/bin/bash -c 'set -- $(findmnt -rn -M /srv/lvmdata -o TARGET,SOURCE,FSTYPE); "
        'test "$1" = /srv/lvmdata && { test "$2" = /dev/mapper/vgdata-lvdata || '
        'test "$2" = /dev/vgdata/lvdata; } && test "$3" = xfs && echo exact\'',
        "exact",
    ),
    (
        "XFS filesystem is healthy and expanded",
        "/usr/bin/sudo /usr/bin/bash -c 'xfs_info /srv/lvmdata >/dev/null && test \"$(df -m "
        "--output=size /srv/lvmdata | tail -1)\" -ge 650 && echo grown'",
        "grown",
    ),
    (
        "recovery evidence is present",
        "/usr/bin/cat /srv/lvmdata/recovery.txt",
        "LX302 recovered and expanded",
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
