#!/usr/bin/python3
# ruff: noqa: E501
"""Bounded, pinned, outcome-based guest checks; root is not an anti-cheat boundary."""

from __future__ import annotations

import ipaddress
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX212"
# Parse effective manager state, never unit-file or learner-script spelling.
TIMER_EVIDENCE = r"""
import re
import subprocess
from decimal import Decimal


def duration(value):
    if not value:
        raise ValueError("missing effective duration")
    if value == "0":
        return Decimal(0)
    units = {"us": 1, "µs": 1, "ms": 1000, "s": 1000000,
             "min": 60000000, "h": 3600000000, "d": 86400000000,
             "w": 604800000000}
    total = Decimal(0)
    while value:
        match = re.match(r"([0-9]+(?:\.[0-9]+)?)(us|µs|ms|min|s|h|d|w)(?:\s*|$)", value)
        if match is None:
            raise ValueError("invalid effective duration")
        total += Decimal(match[1]) * units[match[2]]
        value = value[match.end():]
    return total


result = subprocess.run(
    ["systemctl", "show", "lx212-report.timer", "-p", "TimersMonotonic",
     "-p", "AccuracyUSec", "-p", "RandomizedDelayUSec"],
    check=True, capture_output=True, text=True, timeout=5,
)
timers = {}
properties = {}
for line in result.stdout.splitlines():
    key, separator, value = line.partition("=")
    if separator and key in {"AccuracyUSec", "RandomizedDelayUSec"}:
        if key in properties:
            raise ValueError("duplicate effective property")
        properties[key] = duration(value)
        continue
    match = re.fullmatch(
        r"TimersMonotonic=\{\s*(\w+)=(.*?)\s*;\s*next_elapse=(.*?)\s*\}", line
    )
    if match is None or match[1] in timers:
        raise ValueError("missing, duplicate or invalid effective timer")
    timers[match[1]] = duration(match[2])
if timers != {"OnBootUSec": 10000000, "OnUnitActiveUSec": 10000000}:
    raise ValueError("expected only 10-second boot and active timers")
if properties != {"AccuracyUSec": 1000000, "RandomizedDelayUSec": 0}:
    raise ValueError("expected 1-second accuracy and no randomized delay")
# Empty arrays print no rows, even with --all. Query separately so missing
# scalar/monotonic evidence cannot be confused with an empty calendar array.
calendar = subprocess.run(
    ["systemctl", "show", "lx212-report.timer", "-p", "TimersCalendar", "--value"],
    check=True, capture_output=True, text=True, timeout=5,
)
if calendar.stdout.strip():
    raise ValueError("additional calendar schedules are not allowed")
"""


def timed_command(command: str) -> str:
    return "sudo -n bash -euo pipefail -c " + shlex.quote(
        "python3 -c " + shlex.quote(TIMER_EVIDENCE) + "\n" + command
    )


CHECKS = (
    (
        "learner objective: repaired state survives a reboot",
        'sudo -n bash -euo pipefail -c \'current=$(cat /proc/sys/kernel/random/boot_id)\nprevious=$(cat /var/lib/labctl/lx212-boot)\npattern="^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"\n[[ "$current" =~ $pattern && "$previous" =~ $pattern ]]\ntest "$current" != "$previous"\necho ok\'',
        "ok",
        0,
    ),
    (
        "learner objective: executable transforms fresh inputs",
        'sudo -n bash -euo pipefail -c \'work=$(mktemp -d /var/lib/lx212/check.XXXXXX)\ntrap \'"\'"\'rm -rf "$work"\'"\'"\' EXIT\nnonce=$(cat /proc/sys/kernel/random/uuid)\nprintf "%s\\n" "$nonce previous report" > "$work/output"\nrun_report() {\n    cp "$work/output" "$work/previous"\n    exec 3<"$work/output"\n    timeout 5 /usr/local/sbin/lx212-report "$work/input" "$work/output"\n    test ! "$work/output" -ef /proc/self/fd/3\n    cmp "$work/previous" /proc/self/fd/3\n    exec 3<&-\n    test "$(stat -c "%u:%g %a" "$work/output")" = "0:0 640"\n}\nprintf \'"\'"\'%s\\n\'"\'"\' "ERROR $nonce" \'"\'"\'WARN x\'"\'"\' \'"\'"\'WARN y\'"\'"\' \'"\'"\'INFO z\'"\'"\' \'"\'"\'ERROR again\'"\'"\' \'"\'"\'ERROR last\'"\'"\' > "$work/input"\nrun_report\nhash=$(sha256sum "$work/input" | cut -d\'"\'"\' \'"\'"\' -f1)\nprintf \'"\'"\'ERROR=3\\nWARN=2\\nINFO=1\\nSHA256=%s\\n\'"\'"\' "$hash" > "$work/expected"\ncmp "$work/expected" "$work/output"\n# A second different input rejects a hard-coded count or digest.\nprintf \'"\'"\'%s\\n\'"\'"\' "INFO $nonce" > "$work/input"\nrun_report\nhash=$(sha256sum "$work/input" | cut -d\'"\'"\' \'"\'"\' -f1)\nprintf \'"\'"\'ERROR=0\\nWARN=0\\nINFO=1\\nSHA256=%s\\n\'"\'"\' "$hash" > "$work/expected"\ncmp "$work/expected" "$work/output"\necho ok\'',
        "ok",
        0,
    ),
    (
        "learner objective: timer regenerates the report after reboot",
        timed_command(
            'test "$(systemctl is-enabled lx212-report.timer)" = enabled\ntest "$(systemctl is-active lx212-report.timer)" = active\ntest "$(systemctl show -p Triggers --value lx212-report.timer)" = lx212-report.service\nrm -f /var/lib/lx212/report\nfor attempt in {1..30}; do\n    test ! -f /var/lib/lx212/report || break\n    sleep 1\ndone\nhash=$(sha256sum /var/lib/lx212/events | cut -d\' \' -f1)\nexpected=$(printf \'ERROR=2\\nWARN=1\\nINFO=1\\nSHA256=%s\' "$hash")\ntest "$(cat /var/lib/lx212/report)" = "$expected"\ntest "$(systemctl show -p Result --value lx212-report.service)" = success\ntest "$(stat -c \'%U:%G %a\' /var/lib/lx212/report)" = \'root:root 640\'\necho ok'
        ),
        "ok",
        0,
    ),
)


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("grading context required")
        context = json.loads(Path(sys.argv[1]).read_text())
        if context.get("schema_version") != 1 or context.get("lab_id") != LAB_ID:
            raise ValueError("invalid grading context")
        host = context["hosts"]["node"]
        address = str(ipaddress.ip_address(host["address"]))
        if host["ssh_user"] != "student":
            raise ValueError("student identity required")
        key = Path(host["private_key_path"])
        pins = Path(host["known_hosts_path"])
        ssh = Path(os.environ.get("LABCTL_SSH", "/usr/bin/ssh"))
        if not ssh.is_absolute() or not key.is_file() or not pins.is_file():
            raise ValueError("absolute SSH and pinned credentials required")
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"ERROR {LAB_ID}: {error}", file=sys.stderr)
        return 2
    common = [
        str(ssh),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={pins}",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        "-i",
        str(key),
        "--",
        f"student@{address}",
    ]
    failed = False
    for description, command, expected, expected_rc in CHECKS:
        try:
            argv = [*common, command]
            if command.startswith("http://{address}:"):
                remote_address = f"[{address}]" if ":" in address else address
                argv = [
                    "/usr/bin/curl",
                    "--noproxy",
                    "*",
                    "--connect-timeout",
                    "5",
                    "--max-time",
                    "10",
                    "-fsS",
                    command.format(address=remote_address),
                ]
            result = subprocess.run(  # noqa: S603 - fixed checks and validated address
                argv, text=True, capture_output=True, check=False, timeout=45
            )
            passed = result.returncode == expected_rc and result.stdout.strip() == expected
        except (OSError, subprocess.TimeoutExpired):
            passed = False
        print(f"{'PASS' if passed else 'FAIL'} {LAB_ID}: {description}")
        failed |= not passed
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
