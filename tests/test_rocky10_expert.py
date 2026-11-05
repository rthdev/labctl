# ruff: noqa: S102
"""Expert Rocky 10 lab contracts and transport regression tests (no guests)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from labctl.definitions import load_definition

ROOT = Path(__file__).parents[1] / "src/labctl/data/labs"


def module(lab):
    namespace = {"__name__": "fixture"}
    path = ROOT / lab / "grade.py"
    assert path.exists(), "grader slice not implemented"
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    return namespace


def exercise(tmp_path, lab):
    definition = load_definition(ROOT / lab / "lab.yaml")
    assert definition.vms[0].image == "rocky:10"
    assert "reboot" in definition.instructions.lower()
    assert "EX342" in definition.instructions
    assert os.access(definition.grader, os.X_OK)
    setup = definition.vms[0].setup
    assert os.access(setup, os.X_OK)
    subprocess.run(["/usr/bin/bash", "-n", setup], check=True)
    checks = module(lab)["CHECKS"]
    assert len(checks) >= 6
    for _, command, *_ in checks:
        subprocess.run(["/usr/bin/bash", "-n", "-c", command], check=True)
    ssh = tmp_path / "ssh"
    ssh.write_text("""#!/usr/bin/python3
import json, os, sys
args = sys.argv[1:]
assert 'StrictHostKeyChecking=yes' in args
assert 'ConnectTimeout=5' in args
assert 'BatchMode=yes' in args
assert any(x.startswith('UserKnownHostsFile=') for x in args)
answers = json.loads(os.environ['ANSWERS'])
rc, output = answers[args[-1]]
print(output)
sys.exit(rc)
""")
    ssh.chmod(0o700)
    identity = tmp_path / "key"
    identity.touch()
    hosts = tmp_path / "hosts"
    hosts.touch()
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "lab_id": lab,
                "hosts": {
                    "node": {
                        "address": "192.0.2.1",
                        "ssh_user": "student",
                        "private_key_path": str(identity),
                        "known_hosts_path": str(hosts),
                    }
                },
            }
        )
    )
    answers = {row[1]: [row[3] if len(row) == 4 else 0, row[2]] for row in checks}

    def run(values):
        return subprocess.run(
            [definition.grader, context],
            capture_output=True,
            text=True,
            env={**os.environ, "LABCTL_SSH": str(ssh), "ANSWERS": json.dumps(values)},
            timeout=30,
        )

    result = run(answers)
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == len(checks)
    assert all(line.startswith(f"PASS {lab}:") for line in result.stdout.splitlines())
    for command, (rc, output) in answers.items():
        broken = {**answers, command: [255, output]}
        assert run(broken).returncode == 1
        broken = {**answers, command: [rc, "stale-or-unexpected"]}
        assert run(broken).returncode == 1


@pytest.mark.parametrize(
    "policy,valid",
    [
        ("+ : (lx410-operators) : ALL\n- : ALL : ALL\n", True),
        ("+ : lx410-agent : ALL\n- : ALL : ALL\n", False),
        ("+ : ALL : ALL\n", False),
    ],
)
def test_lx410_admission_policy_is_group_scoped(tmp_path, policy, valid):
    namespace = {}
    exec(module("LX410")["PAM_EVIDENCE"], namespace)
    access = tmp_path / "access"
    access.write_text(policy)
    stack = tmp_path / "stack"
    stack.write_text(
        f"account required pam_unix.so\naccount required pam_access.so accessfile={access}\n"
    )
    if valid:
        namespace["verify_policy"](stack, access)
    else:
        with pytest.raises(AssertionError):
            namespace["verify_policy"](stack, access)


@pytest.mark.parametrize("dependency", ["lx410-admission.service", "unrelated.service"])
def test_lx410_running_units_without_admission_dependency_fail(dependency):
    row = next(row for row in module("LX410")["CHECKS"] if "admission and API" in row[0])
    fixtures = """
systemctl() {
 case "$*" in
 *is-enabled*) echo enabled;;
 *Requires*|*After*) echo "$DEPENDENCY";;
 *User*) echo lx410-app;;
 *MainPID*) echo 123;;
 esac
}
stat() { echo lx410-app; }
"""
    result = subprocess.run(
        ["/usr/bin/bash", "-euo", "pipefail", "-c", fixtures + shlex.split(row[1])[-1]],
        env={**os.environ, "DEPENDENCY": dependency},
        capture_output=True,
    )
    assert (result.returncode == 0) == (dependency == "lx410-admission.service")


@pytest.mark.parametrize("membership", ["lx410-app lx410-readers", "lx410-app"])
def test_lx410_acl_alone_does_not_replace_persistent_reader_membership(membership):
    row = next(row for row in module("LX410")["CHECKS"] if "least privilege" in row[0])
    fixtures = """
sha256sum() { :; }
stat() { echo 'root:lx410-readers 640'; }
runuser() { [[ "$2" == lx410-app ]]; }
id() { echo "$MEMBERSHIP"; }
"""
    result = subprocess.run(
        ["/usr/bin/bash", "-euo", "pipefail", "-c", fixtures + shlex.split(row[1])[-1]],
        env={**os.environ, "MEMBERSHIP": membership},
        capture_output=True,
    )
    assert (result.returncode == 0) == ("lx410-readers" in membership)


def test_lx410(tmp_path):
    exercise(tmp_path, "LX410")


@pytest.mark.parametrize("missing", ["", "lx410-agent", "lx410-outsider", "lx410-expired"])
def test_lx410_canaries_cannot_disappear(missing):
    checks = module("LX410")["CHECKS"]
    rows = [row for row in checks if "canaries" in row[0]]
    assert rows, "missing independent identity validation"
    body = shlex.split(rows[0][1])[-1]
    fixtures = """
getent() {
    if [[ "$2" == "$MISSING" ]]; then return 2; fi
    if [[ "$1" == shadow ]]; then echo 'lx410-expired:!:20000:0:99999:7::1:';
    else echo "$2:x:1001:1001::/:/sbin/nologin"; fi
}
id() { echo 'lx410-operators'; }
"""
    result = subprocess.run(
        ["/usr/bin/bash", "-euo", "pipefail", "-c", fixtures + body],
        env={**os.environ, "MISSING": missing},
        capture_output=True,
    )
    assert (result.returncode == 0) == (not missing)


@pytest.mark.parametrize("lab", ["LX410", "LX411", "LX412"])
def test_missing_boot_baseline_cannot_pass(lab):
    body = shlex.split(module(lab)["CHECKS"][0][1])[-1]
    fixtures = (
        """cat() { if [[ "$1" == /var/lib/* ]]; then return 1; else echo current-boot; fi; }; """
    )
    result = subprocess.run(
        ["/usr/bin/bash", "-euo", "pipefail", "-c", fixtures + body], capture_output=True
    )
    assert result.returncode != 0


@pytest.mark.parametrize("lab", ["LX410", "LX411", "LX412"])
@pytest.mark.parametrize(
    "before,now,valid",
    [
        ("stale", "current", False),
        ("", "current", False),
        ("11111111-1111-4111-8111-111111111111", "11111111-1111-4111-8111-111111111111", False),
        ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", True),
    ],
)
def test_boot_evidence_is_two_distinct_uuids(lab, before, now, valid):
    body = shlex.split(module(lab)["CHECKS"][0][1])[-1]
    fixtures = (
        'cat() { if [[ "$1" == /var/lib/* ]]; then printf "%s" "$BEFORE"; '
        'else printf "%s" "$NOW"; fi; }; '
    )
    result = subprocess.run(
        ["/usr/bin/bash", "-euo", "pipefail", "-c", fixtures + body],
        env={**os.environ, "BEFORE": before, "NOW": now},
        capture_output=True,
    )
    assert (result.returncode == 0) == valid


def test_lx411_traces_the_lv_to_the_encrypted_device():
    checks = module("LX411")["CHECKS"]
    assert any("/sys/class/block/" in row[1] and "/slaves/" in row[1] for row in checks)


@pytest.mark.parametrize("lab", ["LX410", "LX411", "LX412"])
@pytest.mark.parametrize("copied", [False, True])
def test_http_evidence_rejects_copied_docroot(tmp_path, lab, copied):
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from urllib.error import HTTPError

    root = tmp_path / "original"
    copy = tmp_path / "copy"
    root.mkdir()
    copy.mkdir()
    for directory in (root, copy):
        (directory / "data.txt").write_bytes(b"preserved\n")
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(copy if copied else root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        namespace = {}
        exec(module(lab)["HTTP_EVIDENCE"], namespace)
        verify = namespace["verify_http"]
        url = f"http://127.0.0.1:{server.server_port}"
        if copied:
            with pytest.raises(HTTPError):
                verify(str(root), url, "data.txt")
        else:
            verify(str(root), url, "data.txt")
        assert sorted(p.name for p in root.iterdir()) == ["data.txt"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("lab", ["LX410", "LX411", "LX412"])
def test_listener_evidence_is_owned_by_service_pid(lab):
    import socket

    namespace = {}
    exec(module(lab)["SERVICE_EVIDENCE"], namespace)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        namespace["verify_listener"](os.getpid(), listener.getsockname()[1])
        with pytest.raises(AssertionError):
            namespace["verify_listener"](os.getppid(), listener.getsockname()[1])


@pytest.mark.parametrize("lab", ["LX410", "LX411", "LX412"])
@pytest.mark.parametrize("changed", [False, True])
def test_supplied_program_integrity_is_graded(tmp_path, lab, changed):
    import hashlib

    script = tmp_path / "program"
    script.write_bytes(b"original program")
    baseline = tmp_path / "program.sha256"
    baseline.write_text(f"{hashlib.sha256(script.read_bytes()).hexdigest()}  {script}\n")
    if changed:
        script.write_bytes(b"static replacement")
    rows = [row for row in module(lab)["CHECKS"] if "supplied program" in row[0]]
    assert rows, "program replacement currently bypasses the intended objective"
    body = shlex.split(rows[0][1])[-1].replace(
        f"/var/lib/{lab.lower()}/program.sha256", str(baseline)
    )
    result = subprocess.run(["/usr/bin/bash", "-euo", "pipefail", "-c", body], capture_output=True)
    assert (result.returncode == 0) == (not changed)
    assert "program.sha256" in (ROOT / lab / "setup.sh").read_text()


@pytest.mark.parametrize("changed", [False, True])
def test_lx411_recreated_storage_identities_fail(tmp_path, changed):
    baseline = tmp_path / "stack-ids"
    baseline.write_text("pv-original vg-original\nlv-original\nfs-original\n")
    rows = [row for row in module("LX411")["CHECKS"] if "original PV VG LV" in row[0]]
    assert rows, "same LUKS container must not hide reformatted/recreated inner storage"
    body = shlex.split(rows[0][1])[-1].replace("/var/lib/lx411/stack-ids", str(baseline))
    fixtures = """
pvs() { echo 'pv-original vg-original'; }
lvs() { echo "$LV_ID"; }
blkid() { echo fs-original; }
"""
    result = subprocess.run(
        ["/usr/bin/bash", "-euo", "pipefail", "-c", fixtures + body],
        env={**os.environ, "LV_ID": "lv-new" if changed else "lv-original"},
        capture_output=True,
    )
    assert (result.returncode == 0) == (not changed)


def test_lx411_lvm_commands_scope_discovery_to_the_lab_mapper():
    setup = (ROOT / "LX411/setup.sh").read_text()
    commands = ("pvcreate", "vgcreate", "lvcreate", "lvchange", "vgchange")
    lines = [line for line in setup.splitlines() if line.startswith(commands)]
    assert lines
    for line in lines:
        assert "--devices /dev/mapper/lx411-crypt" in line, line


@pytest.mark.parametrize("missing", [False, True])
def test_lx411_boot_dependency_graph_is_effective(missing):
    row = next(row for row in module("LX411")["CHECKS"] if "storage mount and API" in row[0])
    body = shlex.split(row[1])[-1]
    body = body.replace("test /proc/$pid/cwd -ef /srv/lx411", ":")
    body = body.replace("test -f /srv/lx411/service-write-test", ":")
    fixtures = """
systemctl() {
 case "$*" in
 *is-enabled*) echo enabled;;
 *MainPID*) echo 123;;
 *Requires*|*After*)
   if [[ "$MISSING" == yes ]]; then echo unrelated.service;
   elif [[ "$*" == *"show lx411-api"* ]]; then echo srv-lx411.mount;
   else echo lx411-stack.service; fi;;
 esac
}
"""
    result = subprocess.run(
        ["/usr/bin/bash", "-euo", "pipefail", "-c", fixtures + body],
        env={**os.environ, "MISSING": "yes" if missing else "no"},
        capture_output=True,
    )
    assert (result.returncode == 0) == (not missing)


@pytest.mark.parametrize("lab", ["LX410", "LX411", "LX412"])
@pytest.mark.parametrize("mode", ["enforcing", "permissive", "disabled"])
def test_enforcement_must_also_be_persistent(tmp_path, lab, mode):
    config = tmp_path / "selinux-config"
    config.write_text(f"SELINUX={mode}\n")
    row = next(row for row in module(lab)["CHECKS"] if "SELinux baseline" in row[0])
    body = shlex.split(row[1])[-1].replace("/etc/selinux/config", str(config))
    result = subprocess.run(
        ["/usr/bin/bash", "-euo", "pipefail", "-c", "getenforce() { echo Enforcing; }; " + body],
        capture_output=True,
    )
    assert (result.returncode == 0) == (mode == "enforcing")


def test_lx411(tmp_path):
    exercise(tmp_path, "LX411")


def test_lx411_api_proves_a_fresh_write_per_request(tmp_path):
    import socket
    import time
    from urllib.request import Request, urlopen

    setup = (ROOT / "LX411/setup.sh").read_text()
    source = setup.split("cat > /usr/local/sbin/lx411-api.py <<'EOF'\n", 1)[1].split("\nEOF", 1)[0]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = tmp_path / "api.py"
    script.write_text(source.replace("8411", str(port)))
    (tmp_path / "ledger.csv").write_bytes(b"ledger\n")
    process = subprocess.Popen(
        ["/usr/bin/python3", script],
        env={**os.environ, "DATA_ROOT": str(tmp_path)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                with urlopen(
                    Request(
                        f"http://127.0.0.1:{port}/ledger.csv",
                        headers={"X-Labctl-Probe": "fresh-evidence"},
                    ),
                    timeout=1,
                ) as response:
                    assert response.read() == b"ledger\n"
                break
            except OSError:
                time.sleep(0.02)
        else:
            pytest.fail("test API did not become ready")
        assert (tmp_path / "service-write-test").read_text() == "fresh-evidence\n"
        row = next(row for row in module("LX411")["CHECKS"] if "freshly writes" in row[0])
        command = (
            shlex.split(row[1])[-1]
            .replace("/srv/lx411", str(tmp_path))
            .replace(":8411/", f":{port}/")
        )
        result = subprocess.run(["/usr/bin/python3", "-c", command], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    finally:
        process.terminate()
        process.wait(timeout=3)


@pytest.mark.parametrize("dynamic", [False, True])
def test_lx412_workload_requires_current_backend_processing(tmp_path, dynamic):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if dynamic:
                self.send_header("X-Labctl-Probe", self.headers["X-Labctl-Probe"])
            self.end_headers()
            self.wfile.write(b"450\n")

    records = tmp_path / "records"
    records.write_text("125\n250\n75\n")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ns = {}
        exec(module("LX412")["WORKLOAD_EVIDENCE"], ns)
        args = (f"http://127.0.0.1:{server.server_port}/api/total", records)
        if dynamic:
            ns["verify_workload"](*args)
        else:
            with pytest.raises(AssertionError):
                ns["verify_workload"](*args)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("actual", ["original", "copy"])
def test_lx412_apache_request_evidence_rejects_file_aliases(tmp_path, actual):
    original = tmp_path / "original"
    copy = tmp_path / "copy"
    original.write_bytes(b"same bytes")
    copy.write_bytes(b"same bytes")
    log = tmp_path / "access.log"
    log.write_text(f"fresh-token {tmp_path / actual} {os.getpid()}\n")
    ns = {}
    exec(module("LX412")["APACHE_EVIDENCE"], ns)
    if actual == "original":
        ns["verify_apache_record"](log, "fresh-token", original)
    else:
        with pytest.raises(AssertionError):
            ns["verify_apache_record"](log, "fresh-token", original)
    with pytest.raises(AssertionError):
        ns["verify_apache_record"](log, "stale-token", original)


def test_worker_retention_and_calculation(tmp_path):
    setup = ROOT / "LX412/setup.sh"
    assert setup.exists(), "worker not implemented"
    source = setup.read_text().split("# BEGIN WORKER\n", 1)[1].split("# END WORKER", 1)[0]
    ns = {"__name__": "fixture"}
    exec(compile(source, "worker.py", "exec"), ns)
    records = tmp_path / "records.csv"
    records.write_text("10\n-2\n7\n")
    healthy = ns["Ledger"](records, 2)
    broken = ns["Ledger"](records, 128)
    for _ in range(96):
        assert healthy.response() == b"15\n"
        assert broken.response() == b"15\n"
    assert len(healthy.history) == 2
    assert len(broken.history) == 96
    records.write_text("3\n")
    assert healthy.response() == b"3\n", "must read actual records, not a canned response"


@pytest.mark.parametrize("permissive", [False, True])
def test_permissive_apache_is_rejected(permissive):
    row = next(row for row in module("LX412")["CHECKS"] if "confined Apache" in row[0])
    body = shlex.split(row[1])[-1]
    fixtures = """
curl() { :; }
cmp() { :; }
systemctl() { echo 123; }
grep() { if [[ "$*" == *attr/current* ]]; then return 0; fi; /usr/bin/grep "$@"; }
semanage() { printf '%s\\n' "$DOMAIN"; }
"""
    result = subprocess.run(
        ["/usr/bin/bash", "-euo", "pipefail", "-c", fixtures + body],
        env={**os.environ, "DOMAIN": "httpd_t" if permissive else ""},
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is not permissive


def test_lx412(tmp_path):
    exercise(tmp_path, "LX412")


@pytest.mark.parametrize("lab", ["LX410", "LX411", "LX412"])
def test_transport_timeout_is_failure(monkeypatch, tmp_path, capsys, lab):
    namespace = module(lab)
    key = tmp_path / "key"
    key.touch()
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "lab_id": lab,
                "hosts": {
                    "node": {
                        "address": "192.0.2.1",
                        "ssh_user": "student",
                        "private_key_path": str(key),
                        "known_hosts_path": str(key),
                    }
                },
            }
        )
    )
    monkeypatch.setattr("sys.argv", ["grade.py", str(context)])

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] <= 30
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    assert namespace["main"]() == 1
    assert f"FAIL {lab}:" in capsys.readouterr().out
