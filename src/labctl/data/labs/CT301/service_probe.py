"""Read-only Apache static-service proof, executed with guest Python, not on the host.

The explicit server scope avoids modifying learner data to challenge a response.
Do not import this file via a loader that writes bytecode into the lab snapshot.
"""

# ruff: noqa: S603, S108 -- paths inside the learner container, never host writes
from __future__ import annotations

import os
import posixpath
import subprocess
import time

HTTPD_CONFIG = """ServerRoot "/etc/httpd"
LoadModule mpm_event_module modules/mod_mpm_event.so
LoadModule unixd_module modules/mod_unixd.so
LoadModule authz_core_module modules/mod_authz_core.so
Listen 8080
ServerName localhost
PidFile /tmp/ct-httpd.pid
ErrorLog /dev/stderr
DocumentRoot "/var/www/html"
<Directory "/var/www/html">
    Options FollowSymLinks
    AllowOverride None
    Require all granted
</Directory>
"""

_deadline = float("inf")


def run(*args: str) -> str:
    remaining = _deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("service proof deadline")
    return subprocess.run(
        ["/usr/bin/podman", *args], check=True, capture_output=True, timeout=min(2, remaining)
    ).stdout.decode("utf-8")


def verify(container: str, mounted: str, source: str, endpoint: str, expected: str) -> bool:
    def inside(*args: str) -> str:
        return run("exec", container, *args)

    # Read the original backing file independently, then compare inode identity
    # across the backing path, the effective mount, and the server's docroot.
    if run("unshare", "readlink", "-f", "--", source).strip() != source:
        return False
    if run("unshare", "cat", "--", source) != expected:
        return False
    identity = run("unshare", "stat", "-Lc", "%d:%i", "--", source).strip()
    if not identity or inside("cat", mounted) != expected:
        return False
    if inside("stat", "-Lc", "%d:%i", "--", mounted).strip() != identity:
        return False

    table = inside("cat", "/proc/net/tcp", "/proc/net/tcp6")
    listeners = set()
    for line in table.splitlines():
        fields = line.split()
        if len(fields) >= 10 and fields[3] == "0A" and fields[1].endswith(":1F90"):
            listeners.add("socket:[" + fields[9] + "]")
    if not listeners:
        return False
    # `top` selects the named container's processes, even in a shared PID namespace.
    pids = run("top", container, "pid").splitlines()[1:]
    if not 1 <= len(pids) <= 32:
        return False
    # Configuration bytes alone do not establish what an existing daemon loaded.
    # Require both modification/change times to predate EVERY Apache process,
    # including the parent (a newly forked worker must not conceal stale state).
    # Two seconds cover integral stat/btime rounding and process tick granularity.
    # This is conservative educational evidence, not hostile-tamper attestation.
    metadata_args = ("stat", "-Lc", "%d|%i|%s|%Y|%Z|%y|%z", "--", "/tmp/ct-httpd.conf")
    metadata = inside(*metadata_args)
    fields = metadata.strip().split("|")
    if len(fields) != 7:
        return False
    changed = max(int(fields[3]), int(fields[4]))
    boot = [
        line.split()[1]
        for line in inside("cat", "/proc/stat").splitlines()
        if line.startswith("btime ")
    ]
    if len(boot) != 1 or inside("cat", "/tmp/ct-httpd.conf") != HTTPD_CONFIG:
        return False
    ticks = os.sysconf("SC_CLK_TCK")
    owned = set()
    starts = {}
    for raw_pid in pids:
        pid = raw_pid.strip()
        if not pid.isdecimal():
            return False
        args = inside("cat", "/proc/" + pid + "/cmdline").rstrip("\0").split("\0")
        if not args or posixpath.basename(args[0]) != "httpd":
            continue
        if inside("readlink", "/proc/" + pid + "/exe").strip() != "/usr/sbin/httpd":
            return False
        # Deliberately scoped static configuration: no includes, rewrites, CGI,
        # aliases, per-directory overrides, or command-line injected directives.
        if args[1:] not in (
            ["-f", "/tmp/ct-httpd.conf", "-DFOREGROUND"],
            ["-f", "/tmp/ct-httpd.conf", "-D", "FOREGROUND"],
        ):
            return False
        # comm may contain spaces/parentheses; fields after its final ')' start
        # at field 3, so starttime (field 22) is index 19.
        start = inside("cat", "/proc/" + pid + "/stat").rsplit(")", 1)[1].split()[19]
        if changed + 2 >= int(boot[0]) + int(start) / ticks:
            return False
        starts[pid] = start
        served = "/proc/" + pid + "/root/var/www/html/" + endpoint
        if inside("stat", "-Lc", "%d:%i", "--", served).strip() != identity:
            return False
        fds = inside(
            "sh", "-ec", 'for f in /proc/"$1"/fd/*; do readlink "$f" || :; done', "sh", pid
        ).splitlines()
        owned.update(listeners.intersection(fds))
    if owned != listeners or not starts:
        return False
    # Catch ordinary concurrent replacement/edit and PID reuse during the proof.
    # No writes, reloads, or requests that mutate learner data are performed.
    for pid, start in starts.items():
        if inside("cat", "/proc/" + pid + "/stat").rsplit(")", 1)[1].split()[19] != start:
            return False
    return inside(*metadata_args) == metadata


def main(container: str, mounted: str, source: str, endpoint: str, expected: str) -> None:
    # Expire before the 15s SSH parent; subprocess.run kills/reaps timed-out children.
    global _deadline
    _deadline = time.monotonic() + 12
    try:
        ok = verify(container, mounted, source, endpoint, expected)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        ok = False
    print("connected" if ok else "unconnected")
    raise SystemExit(0 if ok else 1)
