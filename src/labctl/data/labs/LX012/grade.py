#!/usr/bin/python3
# ruff: noqa: E501
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX012"
CHECKS = {
    "message content and permissions": (
        "/usr/bin/sudo -n /usr/bin/bash -o pipefail -c 'if { test -f /home/student/lx012/message.txt && test ! -L /home/student/lx012/message.txt && test -f /home/student/lx012/message.txt && test ! -L /home/student/lx012/message.txt && test \"$(stat -c '\"'\"'%U:%G %a'\"'\"' /home/student/lx012/message.txt)\" = '\"'\"'student:student 640'\"'\"' && printf '\"'\"'Rocky archive practice\\n'\"'\"' | cmp -s - /home/student/lx012/message.txt; }; then printf \"ok\\n\"; else exit 1; fi'",
        "ok",
    ),
    "hard link identity": (
        "/usr/bin/sudo -n /usr/bin/bash -o pipefail -c 'if { test ! -L /home/student/lx012/message.hard && test /home/student/lx012/message.txt -ef /home/student/lx012/message.hard; }; then printf \"ok\\n\"; else exit 1; fi'",
        "ok",
    ),
    "symbolic link target": (
        "/usr/bin/sudo -n /usr/bin/bash -o pipefail -c 'if { test -L /home/student/lx012/current && test /home/student/lx012/current -ef /home/student/lx012/message.txt; }; then printf \"ok\\n\"; else exit 1; fi'",
        "ok",
    ),
    "gzip archive contents and mode": (
        "/usr/bin/sudo -n /usr/bin/bash -o pipefail -c 'if { test -f /home/student/lx012/backup.tar.gz && test ! -L /home/student/lx012/backup.tar.gz && python3 -c '\"'\"'import tarfile; t=tarfile.open('\"'\"'\"'\"'\"'\"'\"'\"'/home/student/lx012/backup.tar.gz'\"'\"'\"'\"'\"'\"'\"'\"','\"'\"'\"'\"'\"'\"'\"'\"'r:gz'\"'\"'\"'\"'\"'\"'\"'\"'); m=t.getmembers(); assert len(m)==1 and m[0].name=='\"'\"'\"'\"'\"'\"'\"'\"'message.txt'\"'\"'\"'\"'\"'\"'\"'\"' and m[0].isfile() and m[0].mode==0o640; assert t.extractfile(m[0]).read()==b'\"'\"'\"'\"'\"'\"'\"'\"'Rocky archive practice\\n'\"'\"'\"'\"'\"'\"'\"'\"''\"'\"'; }; then printf \"ok\\n\"; else exit 1; fi'",
        "ok",
    ),
}


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("grading context path must be the first argument")
        context_path = Path(sys.argv[1])
        context = json.loads(context_path.read_text(encoding="utf-8"))
        if context.get("schema_version") != 1 or context.get("lab_id") != LAB_ID:
            raise ValueError("grading context does not describe LX012 schema v1")
        host = context["hosts"]["node"]
        address = host["address"]
        if not isinstance(address, str) or not address or any(char.isspace() for char in address):
            raise ValueError("node address is invalid")
        ssh = Path(os.environ.get("LABCTL_SSH", "/usr/bin/ssh"))
        identity = Path(host["private_key_path"])
        known_hosts = Path(host["known_hosts_path"])
        if not ssh.is_absolute() or not identity.is_file() or not known_hosts.is_file():
            raise ValueError(
                "absolute SSH executable, identity, and pinned known_hosts are required"
            )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {LAB_ID}: {error}", file=sys.stderr)
        return 2

    failed = False
    common = [
        str(ssh),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
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
    for description, (command, expected) in CHECKS.items():
        # All destinations and commands are validated or fixed above; no shell is used.
        try:
            result = subprocess.run(  # noqa: S603
                [*common, command], text=True, capture_output=True, check=False, timeout=30
            )
            passed = result.returncode == 0 and result.stdout.strip() == expected
        except (OSError, subprocess.TimeoutExpired):
            passed = False
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {description}")
        failed = failed or not passed
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
