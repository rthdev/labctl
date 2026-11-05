#!/usr/bin/python3
# ruff: noqa: E501, S603
"""Bounded incident checks, including temporary HTTP evidence probes."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX411"
HTTP_EVIDENCE = r"""
import os
import secrets
import tempfile
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


def verify_http(root, url, filename):
    root = Path(root)
    original = root / filename
    assert not original.is_symlink()
    assert original.stat().st_dev == root.stat().st_dev
    opener = build_opener(ProxyHandler({}))
    def fetch(name):
        with opener.open(url + '/' + name, timeout=3) as response:
            assert response.status == 200
            return response.read(1048576)
    before = original.stat()
    assert fetch(filename) == original.read_bytes()
    fd, name = tempfile.mkstemp(prefix='labctl-probe-', dir=root)
    probe = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(secrets.token_bytes(48))
        probe.chmod(0o644)
        assert fetch(probe.name) == probe.read_bytes()
        after = original.stat()
        assert (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
    finally:
        probe.unlink()
"""
SERVICE_EVIDENCE = r"""
from pathlib import Path
import subprocess


def verify_listener(pid, port):
    assert pid > 0
    sockets = set()
    for fd in Path(f'/proc/{pid}/fd').iterdir():
        try:
            sockets.add(str(fd.readlink()))
        except FileNotFoundError:
            pass
    found = []
    for table in ('tcp', 'tcp6'):
        for line in Path('/proc/net/' + table).read_text().splitlines()[1:]:
            fields = line.split()
            address, number = fields[1].split(':')
            if int(number, 16) == port and fields[3] == '0A':
                assert table == 'tcp' and address == '0100007F'
                found.append('socket:[' + fields[9] + ']')
    assert found and set(found) <= sockets


def verify_service(unit, port, program):
    pid = int(subprocess.check_output(['systemctl', 'show', unit, '-p', 'MainPID', '--value'], text=True, timeout=3))
    verify_listener(pid, port)
    args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    assert program.encode() in args
    return pid
"""
CHECKS = (
    (
        "repaired state survived a reboot",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'before=$(cat /var/lib/lx411/boot-id); now=$(cat /proc/sys/kernel/random/boot_id); [[ "$before" =~ ^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$ ]]; [[ "$now" =~ ^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$ ]]; test "$now" != "$before"; echo verified\'',
        "verified",
    ),
    (
        "SELinux baseline remains enforcing",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'test "$(getenforce)" = Enforcing; python3 -c \'"\'"\'import re, shlex; from pathlib import Path; values = [shlex.split(line.split("=", 1)[1], comments=True) for line in Path("/etc/selinux/config").read_text().splitlines() if re.match(r"^\\s*SELINUX\\s*=", line)]; assert values == [["enforcing"]]\'"\'"\'; echo verified\'',
        "verified",
    ),
    (
        "original LUKS container is open on the supplied backing image",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'loop=$(losetup -j /var/lib/lx411/ledger.img --output NAME --noheadings); test -n "$loop"; test "$(cryptsetup luksUUID "$loop")" = "$(cat /var/lib/lx411/luks-uuid)"; actual=$(cryptsetup status lx411-crypt | awk \'"\'"\'$1 == "device:" {print $2}\'"\'"\'); test "$(readlink -f "$actual")" = "$(readlink -f "$loop")"; echo verified\'',
        "verified",
    ),
    (
        "mounted ext4 filesystem is the writable ledger LV",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'test "$(findmnt -rn -M /srv/lx411 -o FSTYPE)" = ext4; test "$(readlink -f "$(findmnt -rn -M /srv/lx411 -o SOURCE)")" = "$(readlink -f /dev/lx411-vg/ledger)"; findmnt -rn -M /srv/lx411 -o OPTIONS | tr , \'"\'"\'\\n\'"\'"\' | grep -qx rw; test "$(blockdev --getro /dev/lx411-vg/ledger)" = 0; echo verified\'',
        "verified",
    ),
    (
        "ledger VG has exactly the unlocked lab PV",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'pv=$(pvs --noheadings -o pv_name --select vg_name=lx411-vg --devices /dev/mapper/lx411-crypt | xargs); test "$pv" = /dev/mapper/lx411-crypt; test "$(lvs --devices /dev/mapper/lx411-crypt --noheadings -o lv_name lx411-vg | xargs)" = ledger; echo verified\'',
        "verified",
    ),
    (
        "active LV has only the encrypted mapper as its backing device",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'lv=$(basename "$(readlink -f /dev/lx411-vg/ledger)"); crypt=$(basename "$(readlink -f /dev/mapper/lx411-crypt)"); set -- /sys/class/block/"$lv"/slaves/*; test "$#" = 1; test -e "$1"; test "$(basename "$1")" = "$crypt"; echo verified\'',
        "verified",
    ),
    (
        "original ledger data remains intact",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c 'sha256sum -c /var/lib/lx411/data.sha256 >/dev/null; echo verified'",
        "verified",
    ),
    (
        "storage mount and API recovered persistently",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'for u in lx411-stack srv-lx411.mount lx411-api; do test "$(systemctl is-enabled "$u")" = enabled; systemctl is-active --quiet "$u"; done; pid=$(systemctl show lx411-api -p MainPID --value); test /proc/$pid/cwd -ef /srv/lx411; test -f /srv/lx411/service-write-test; for property in Requires After; do systemctl show lx411-api -p "$property" --value | tr " " "\\n" | grep -qx srv-lx411.mount; systemctl show srv-lx411.mount -p "$property" --value | tr " " "\\n" | grep -qx lx411-stack.service; done; echo verified\'',
        "verified",
    ),
    (
        "live API delivers original mounted ledger",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c 'curl --max-time 5 -fsS http://127.0.0.1:8411/ledger.csv | cmp - /srv/lx411/ledger.csv; echo verified'",
        "verified",
    ),
    (
        "recovery key retains restricted permissions",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'test "$(stat -c "%U:%G %a" /root/lx411-recovery.key)" = "root:root 600"; echo verified\'',
        "verified",
    ),
    (
        "current HTTP docroot is the preserved filesystem, not a copy",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/python3 -c "
        + shlex.quote(
            HTTP_EVIDENCE
            + "\nverify_http('/srv/lx411', \"http://127.0.0.1:8411\", 'ledger.csv')\nprint(\"verified\")"
        ),
        "verified",
    ),
    (
        "API freshly writes the recovered mount",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/python3 -c "
        + shlex.quote(
            "from pathlib import Path\nimport secrets\nfrom urllib.request import Request, ProxyHandler, build_opener\nroot = Path('/srv/lx411')\nreceipt = root / 'service-write-test'\nassert not receipt.is_symlink()\ntoken = secrets.token_hex(24)\nrequest = Request('http://127.0.0.1:8411/ledger.csv', headers={'X-Labctl-Probe': token})\nwith build_opener(ProxyHandler({})).open(request, timeout=3) as response:\n    assert response.read() == (root / 'ledger.csv').read_bytes()\nassert receipt.read_text() == token + '\\n'\nassert receipt.stat().st_dev == root.stat().st_dev\nprint('verified')\n"
        ),
        "verified",
    ),
    (
        "live listeners belong to the required service programs",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/python3 -c "
        + shlex.quote(
            SERVICE_EVIDENCE
            + "\nverify_service('lx411-api', 8411, '/usr/local/sbin/lx411-api.py')\nprint('verified')"
        ),
        "verified",
    ),
    (
        "supplied program bytes remain unchanged",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c 'sha256sum -c /var/lib/lx411/program.sha256 >/dev/null; echo verified'",
        "verified",
    ),
    (
        "original PV VG LV and filesystem identities survive",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'actual=$(pvs --devices /dev/mapper/lx411-crypt --noheadings -o pv_uuid,vg_uuid /dev/mapper/lx411-crypt | xargs; lvs --devices /dev/mapper/lx411-crypt --noheadings -o lv_uuid /dev/lx411-vg/ledger | xargs; blkid -s UUID -o value /dev/lx411-vg/ledger); test -n "$actual"; test "$actual" = "$(cat /var/lib/lx411/stack-ids)"; echo verified\'',
        "verified",
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
        address = str(ipaddress.ip_address(host["address"]))
        user = host["ssh_user"]
        if not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
            raise ValueError("invalid SSH user")
        identity, known = Path(host["private_key_path"]), Path(host["known_hosts_path"])
        ssh = Path(os.environ.get("LABCTL_SSH", "/usr/bin/ssh"))
        if not ssh.is_absolute() or not identity.is_file() or not known.is_file():
            raise ValueError("absolute SSH and pinned credentials required")
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"ERROR {LAB_ID}: {error}", file=sys.stderr)
        return 2
    common = [
        str(ssh),
        "-n",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known}",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        "-i",
        str(identity),
        "--",
        f"{user}@{address}",
    ]
    failed = False
    for row in CHECKS:
        label, command, expected = row[:3]
        code = row[3] if len(row) == 4 else 0
        try:
            result = subprocess.run(
                [*common, command], text=True, capture_output=True, check=False, timeout=25
            )
            passed = result.returncode == code and result.stdout.strip() == expected
        except (OSError, subprocess.TimeoutExpired):
            passed = False
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {label}")
        failed |= not passed
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
