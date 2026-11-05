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

LAB_ID = "LX412"
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
WORKLOAD_EVIDENCE = r"""
from pathlib import Path
import secrets
from urllib.request import Request, ProxyHandler, build_opener


def verify_workload(url, records):
    expected = (str(sum(map(int, Path(records).read_text().splitlines()))) + '\n').encode()
    opener = build_opener(ProxyHandler({}))
    for _ in range(96):
        token = secrets.token_hex(24)
        request = Request(url, headers={'X-Labctl-Probe': token})
        with opener.open(request, timeout=1) as response:
            assert response.status == 200
            assert response.headers.get('X-Labctl-Probe') == token
            assert response.read(1024) == expected
"""
APACHE_EVIDENCE = r"""
from pathlib import Path
import os


def verify_apache_record(log, token, original):
    with Path(log).open('rb') as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 65536))
        rows = [line.split() for line in stream.read().decode().splitlines() if line.startswith(token + ' ')]
    assert len(rows) == 1 and len(rows[0]) == 3
    _, filename, pid = rows[0]
    assert filename == str(original) and pid.isdecimal() and int(pid) > 0
    assert os.path.samefile(f'/proc/{pid}/root{filename}', original)
    return pid
"""
CHECKS = (
    (
        "repaired state survived a reboot",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'before=$(cat /var/lib/lx412/boot-id); now=$(cat /proc/sys/kernel/random/boot_id); [[ "$before" =~ ^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$ ]]; [[ "$now" =~ ^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$ ]]; test "$now" != "$before"; echo verified\'',
        "verified",
    ),
    (
        "SELinux baseline remains enforcing",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'test "$(getenforce)" = Enforcing; python3 -c \'"\'"\'import re, shlex; from pathlib import Path; values = [shlex.split(line.split("=", 1)[1], comments=True) for line in Path("/etc/selinux/config").read_text().splitlines() if re.match(r"^\\s*SELINUX\\s*=", line)]; assert values == [["enforcing"]]\'"\'"\'; echo verified\'',
        "verified",
    ),
    (
        "original deployment and records preserved",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c 'sha256sum -c /var/lib/lx412/data.sha256 >/dev/null; echo verified'",
        "verified",
    ),
    (
        "static artifact delivered by confined Apache",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c 'curl --max-time 3 -fsS http://127.0.0.1:8412/release.txt | cmp - /srv/lx412/public/release.txt; pid=$(systemctl show httpd -p MainPID --value); grep -q '\"'\"':httpd_t:'\"'\"' /proc/$pid/attr/current; domains=$(semanage permissive -l); if grep -qw httpd_t <<< \"$domains\"; then exit 1; else test \"$?\" -eq 1; fi; echo verified'",
        "verified",
    ),
    (
        "content labels are correct on disk and in persistent policy",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c 'for p in /srv/lx412/public /srv/lx412/public/release.txt; do stat -c %C \"$p\" | grep -q '\"'\"':httpd_sys_content_t:'\"'\"'; matchpathcon -n \"$p\" | grep -q '\"'\"':httpd_sys_content_t:'\"'\"'; done; echo verified'",
        "verified",
    ),
    (
        "proxy workload is correct without growth or worker replacement",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/python3 -c "
        + shlex.quote(
            SERVICE_EVIDENCE
            + WORKLOAD_EVIDENCE
            + "\npid = verify_service('lx412-worker', 9412, '/usr/local/sbin/lx412-worker.py')\nstart = Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()[19]\ncg = subprocess.check_output(['systemctl', 'show', 'lx412-worker', '-p', 'ControlGroup', '--value'], text=True, timeout=3).strip()\nassert cg.startswith('/') and cg != '/'\nbase = Path('/sys/fs/cgroup' + cg)\nassert (base / 'memory.max').read_text().strip() == '100663296'\nverify_workload('http://127.0.0.1:8412/api/total', '/srv/lx412/records.csv')\nassert verify_service('lx412-worker', 9412, '/usr/local/sbin/lx412-worker.py') == pid\nassert Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()[19] == start\nassert int((base / 'memory.current').read_text()) < 50331648\nprint('verified')\n"
        ),
        "verified",
    ),
    (
        "both services are enabled and active after reboot",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'for u in httpd lx412-worker; do test "$(systemctl is-enabled "$u")" = enabled; systemctl is-active --quiet "$u"; done; echo verified\'',
        "verified",
    ),
    (
        "both lab listeners remain loopback-only",
        '/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c \'for port in 8412 9412; do endpoints=$(ss -H -ltn "sport = :$port" | awk \'"\'"\'{print $4}\'"\'"\'); test "$endpoints" = "127.0.0.1:$port"; done; echo verified\'',
        "verified",
    ),
    (
        "persistent journal holds current-boot diagnostics for both units",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c 'journalctl --directory=/var/log/journal --quiet -b -u httpd -n 1 | grep -q .; journalctl --directory=/var/log/journal --quiet -b -u lx412-worker -n 1 | grep -q .; echo verified'",
        "verified",
    ),
    (
        "current HTTP docroot is the preserved filesystem, not a copy",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/python3 -c "
        + shlex.quote(
            HTTP_EVIDENCE
            + "\nverify_http('/srv/lx412/public', \"http://127.0.0.1:8412\", 'release.txt')\nprint(\"verified\")"
        ),
        "verified",
    ),
    (
        "live listeners belong to the required service programs",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/python3 -c "
        + shlex.quote(
            SERVICE_EVIDENCE
            + "\nverify_service('httpd', 8412, '/usr/sbin/httpd')\nverify_service('lx412-worker', 9412, '/usr/local/sbin/lx412-worker.py')\nprint('verified')"
        ),
        "verified",
    ),
    (
        "supplied program bytes remain unchanged",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/bash -euo pipefail -c 'sha256sum -c /var/lib/lx412/program.sha256 >/dev/null; echo verified'",
        "verified",
    ),
    (
        "current Apache request resolves the original release inode",
        "/usr/bin/sudo -n /usr/bin/timeout 20 /usr/bin/python3 -c "
        + shlex.quote(
            APACHE_EVIDENCE
            + "\nimport secrets\nimport time\nfrom urllib.request import Request, ProxyHandler, build_opener\ntoken = secrets.token_hex(24)\nrequest = Request('http://127.0.0.1:8412/release.txt', headers={'X-Labctl-Probe': token})\nwith build_opener(ProxyHandler({})).open(request, timeout=3) as response:\n    assert response.read() == Path('/srv/lx412/public/release.txt').read_bytes()\nfor attempt in range(20):\n    try:\n        pid = verify_apache_record('/var/log/httpd/lx412-access.log', token, '/srv/lx412/public/release.txt')\n        break\n    except AssertionError:\n        time.sleep(0.05)\nelse:\n    raise AssertionError('no current Apache filename evidence')\nassert ':httpd_t:' in Path(f'/proc/{pid}/attr/current').read_text()\nassert '/httpd.service' in Path(f'/proc/{pid}/cgroup').read_text()\nprint('verified')\n"
        ),
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
