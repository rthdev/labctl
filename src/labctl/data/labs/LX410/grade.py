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

LAB_ID = "LX410"
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
PAM_EVIDENCE = r"""
from pathlib import Path


def verify_policy(stack, access):
    lines = [line.split() for line in Path(stack).read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]
    assert len(lines) == 2
    modules = {}
    for kind, control, module, *options in lines:
        assert kind == 'account' and control in ('required', 'requisite')
        assert module not in modules
        modules[module] = options
    assert modules == {'pam_unix.so': [], 'pam_access.so': ['accessfile=' + str(access)]}
    rules = [[part.strip() for part in line.split(':')] for line in Path(access).read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]
    assert rules == [['+', '(lx410-operators)', 'ALL'], ['-', 'ALL', 'ALL']]
"""
CHECKS = (
    (
        "repaired state survived a reboot",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'before=$(cat /var/lib/lx410/boot-id); now=$(cat /proc/sys/kernel/random/boot_id); [[ "$before" =~ ^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$ ]]; [[ "$now" =~ ^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$ ]]; test "$now" != "$before"; echo verified\'',
        "verified",
    ),
    (
        "SELinux baseline remains enforcing",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'test "$(getenforce)" = Enforcing; python3 -c \'"\'"\'import re, shlex; from pathlib import Path; values = [shlex.split(line.split("=", 1)[1], comments=True) for line in Path("/etc/selinux/config").read_text().splitlines() if re.match(r"^\\s*SELINUX\\s*=", line)]; assert values == [["enforcing"]]\'"\'"\'; echo verified\'',
        "verified",
    ),
    (
        "PAM account decision for lx410-agent",
        "/usr/bin/sudo -n /usr/bin/timeout 10 /usr/local/libexec/lx410-account lx410-agent",
        "admitted",
        0,
    ),
    (
        "PAM account decision for lx410-outsider",
        "/usr/bin/sudo -n /usr/bin/timeout 10 /usr/local/libexec/lx410-account lx410-outsider",
        "denied",
        1,
    ),
    (
        "PAM account decision for lx410-expired",
        "/usr/bin/sudo -n /usr/bin/timeout 10 /usr/local/libexec/lx410-account lx410-expired",
        "denied",
        1,
    ),
    (
        "account canaries retain independent expiry and group coverage",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c 'for u in lx410-agent lx410-outsider lx410-expired; do getent passwd \"$u\" >/dev/null; done; test \"$(getent shadow lx410-expired | cut -d: -f8)\" = 1; for u in lx410-agent lx410-expired; do id -nG \"$u\" | tr '\"'\"' '\"'\"' '\"'\"'\\n'\"'\"' | grep -qx lx410-operators; done; echo verified'",
        "verified",
    ),
    (
        "dispatch data and least privilege preserved",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'sha256sum -c /var/lib/lx410/data.sha256 >/dev/null; test "$(stat -c "%U:%G %a" /srv/lx410/dispatch.txt)" = "root:lx410-readers 640"; runuser -u lx410-app -- test -r /srv/lx410/dispatch.txt; if runuser -u lx410-outsider -- test -r /srv/lx410/dispatch.txt; then exit 1; fi; id -nG lx410-app | tr " " "\\n" | grep -qx lx410-readers; echo verified\'',
        "verified",
    ),
    (
        "admission and API active and persistent",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'for u in lx410-admission lx410-api; do test "$(systemctl is-enabled "$u")" = enabled; systemctl is-active --quiet "$u"; done; test "$(systemctl show lx410-api -p User --value)" = lx410-app; pid=$(systemctl show lx410-api -p MainPID --value); test "$(stat -c %U /proc/$pid)" = lx410-app; for property in Requires After; do systemctl show lx410-api -p "$property" --value | tr " " "\\n" | grep -qx lx410-admission.service; done; echo verified\'',
        "verified",
    ),
    (
        "live service serves the preserved dispatch data",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c 'curl --max-time 5 -fsS http://127.0.0.1:8410/dispatch.txt | cmp - /srv/lx410/dispatch.txt; echo verified'",
        "verified",
    ),
    (
        "current HTTP docroot is the preserved filesystem, not a copy",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/python3 -c "
        + shlex.quote(
            HTTP_EVIDENCE
            + "\nverify_http('/srv/lx410', \"http://127.0.0.1:8410\", 'dispatch.txt')\nprint(\"verified\")"
        ),
        "verified",
    ),
    (
        "live listeners belong to the required service programs",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/python3 -c "
        + shlex.quote(
            SERVICE_EVIDENCE
            + "\nverify_service('lx410-api', 8410, 'http.server')\nprint('verified')"
        ),
        "verified",
    ),
    (
        "account stack retains expiry AND group authorization",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/python3 -c "
        + shlex.quote(
            PAM_EVIDENCE
            + "\nverify_policy('/etc/pam.d/lx410', '/etc/security/lx410-access.conf')\nprint('verified')"
        ),
        "verified",
    ),
    (
        "supplied program bytes remain unchanged",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c 'sha256sum -c /var/lib/lx410/program.sha256 >/dev/null; echo verified'",
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
