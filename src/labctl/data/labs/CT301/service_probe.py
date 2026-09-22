"""Read-only BusyBox static-service proof, executed with guest Python, not on the host.

The explicit server scope avoids modifying learner data to challenge a response.
Do not import this file via a loader that writes bytecode into the lab snapshot.
"""

# ruff: noqa: S603
from __future__ import annotations

import posixpath
import subprocess
import time

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
    owned = set()
    for raw_pid in pids:
        pid = raw_pid.strip()
        if not pid.isdecimal():
            return False
        args = inside("cat", "/proc/" + pid + "/cmdline").rstrip("\0").split("\0")
        if posixpath.basename(args[0]) == "busybox":
            args = args[1:]
        if not args or posixpath.basename(args[0]) != "httpd":
            continue
        if inside("readlink", "/proc/" + pid + "/exe").strip() != "/bin/busybox":
            return False
        cwd = inside("readlink", "/proc/" + pid + "/cwd").strip()
        root = cwd
        i = 1
        while i < len(args):
            option = args[i]
            if option in {"-f", "-v", "-vv"}:
                i += 1
            elif option in {"-p", "-h"} and i + 1 < len(args):
                if option == "-h":
                    root = args[i + 1]
                i += 2
            else:
                return False
        # BusyBox chdirs to -h before serving. Its current cwd is the effective
        # document root, including when -h was relative at startup.
        root = cwd if not root.startswith("/") else root
        if not root.startswith("/") or not cwd.startswith("/"):
            return False
        inside(
            "sh",
            "-ec",
            'test ! -e /etc/httpd.conf; test ! -e "$1/httpd.conf"; test ! -e "$2/httpd.conf"',
            "sh",
            root,
            cwd,
        )
        served = "/proc/" + pid + "/cwd/" + endpoint
        if inside("stat", "-Lc", "%d:%i", "--", served).strip() != identity:
            return False
        fds = inside(
            "sh", "-ec", 'for f in /proc/"$1"/fd/*; do readlink "$f" || :; done', "sh", pid
        ).splitlines()
        owned.update(listeners.intersection(fds))
    return owned == listeners


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
