#!/usr/bin/python3
# ruff: noqa: E501
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LAB_ID = "LX110"
CHECKS = {
    "operator account and locked password": (
        '/usr/bin/sudo -n /usr/bin/bash -o pipefail -c \'if { getent passwd lxoperator | awk -F: \'"\'"\'$1=="lxoperator" && $6=="/home/lxoperator" && $7=="/bin/bash" {ok=1} END {exit !ok}\'"\'"\' && test -d /home/lxoperator && passwd -S lxoperator | awk \'"\'"\'$2=="L" {ok=1} END {exit !ok}\'"\'"\'; }; then printf "ok\\n"; else exit 1; fi\'',
        "ok",
    ),
    "supplementary team membership": (
        "/usr/bin/sudo -n /usr/bin/bash -o pipefail -c 'if { primary=$(id -gn lxoperator) && test \"$primary\" != lxteam && id -nG lxoperator | tr '\"'\"' '\"'\"' '\"'\"'\\n'\"'\"' | grep -Fxq lxteam && getent group lxteam | awk -F: '\"'\"'{n=split($4,a,\",\"); for(i=1;i<=n;i++) if(a[i]==\"lxoperator\") ok=1} END {exit !ok}'\"'\"'; }; then printf \"ok\\n\"; else exit 1; fi'",
        "ok",
    ),
    "password aging": (
        "/usr/bin/sudo -n /usr/bin/bash -o pipefail -c 'if { getent shadow lxoperator | awk -F: '\"'\"'$5==45 && $6==7 {ok=1} END {exit !ok}'\"'\"'; }; then printf \"ok\\n\"; else exit 1; fi'",
        "ok",
    ),
    "shared directory mode": (
        "/usr/bin/sudo -n /usr/bin/bash -o pipefail -c 'if { test -d /srv/lx110-share && test ! -L /srv/lx110-share && test \"$(stat -c '\"'\"'%U:%G %a'\"'\"' /srv/lx110-share)\" = '\"'\"'root:lxteam 2770'\"'\"'; }; then printf \"ok\\n\"; else exit 1; fi'",
        "ok",
    ),
    "effective scoped sudo": (
        '/usr/bin/sudo -n /usr/bin/bash -o pipefail -c \'if { export LC_ALL=C; groups=$(id -nG lxoperator) && [[ " $groups " != *" wheel "* ]] && sudo -n -l -U lxoperator | python3 -c \'"\'"\'import sys; s=sys.stdin.read(); marker="may run the following commands on"; assert marker in s; rules=s.split(marker,1)[1].split("\\n",1)[1]; lines=[" ".join(x.split()) for x in rules.splitlines() if x.strip()]; import re; pattern=r"\\((?:root|#0)(?: : (?:root|#0))?\\) ((?:(?:NOPASSWD|NOEXEC|NOSETENV|LOG_INPUT|LOG_OUTPUT|INTERCEPT): )+)/usr/bin/id -u"; matches=[re.fullmatch(pattern,x) for x in lines]; assert matches and all(m and "NOPASSWD:" in m[1] for m in matches)\'"\'"\' && visudo -c >/dev/null && test "$(runuser -u lxoperator -- sudo -n -u root /usr/bin/id -u)" = 0 && { sudo -n -l -U lxoperator -u root /usr/bin/cat /etc/shadow >/dev/null 2>&1; test "$?" = 1; }; }; then printf "ok\\n"; else exit 1; fi\'',
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
            raise ValueError("grading context does not describe LX110 schema v1")
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
