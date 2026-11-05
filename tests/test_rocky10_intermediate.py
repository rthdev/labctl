"""Rocky 10 intermediate contracts; no host or guest lifecycle operations."""

# ruff: noqa: S102, S607
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from labctl.definitions import load_definition

LABS = Path(__file__).parents[1] / "src/labctl/data/labs"
IDS = ["LX210", "LX211", "LX212", "LX310", "LX311", "LX312"]


def grader(lab_id):
    path = LABS / lab_id / "grade.py"
    assert path.is_file(), f"missing {lab_id} implementation"
    namespace = {"__name__": "grader_test"}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    return namespace


@pytest.mark.parametrize("lab_id", IDS)
def test_contract_and_shell(lab_id):
    g = grader(lab_id)
    definition = load_definition(LABS / lab_id / "lab.yaml")
    assert definition.vms[0].image == "rocky:10"
    assert len(definition.vms) == 1
    assert f"labctl grade {lab_id}" in definition.instructions
    assert "Forbidden" in definition.instructions
    assert "reboot" in definition.instructions.lower()
    assert os.access(definition.grader, os.X_OK)
    setup = definition.vms[0].setup
    assert os.access(setup, os.X_OK)
    assert subprocess.run(["bash", "-n", str(setup)], check=False).returncode == 0
    assert "LAB.md" not in setup.read_text()
    for _, command, _, _ in g["CHECKS"]:
        if command.startswith("http://{address}:"):
            continue
        tokens = shlex.split(command)
        assert tokens[:5] == ["sudo", "-n", "bash", "-euo", "pipefail"]
        assert subprocess.run(["bash", "-n", "-c", tokens[-1]], check=False).returncode == 0


@pytest.mark.parametrize("lab_id", IDS)
def test_grading_status_transport_and_each_false_positive(lab_id, tmp_path, monkeypatch, capsys):
    g = grader(lab_id)
    key = tmp_path / "id"
    hosts = tmp_path / "known_hosts"
    key.touch()
    hosts.touch()
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "lab_id": lab_id,
                "hosts": {
                    "node": {
                        "address": "192.0.2.10",
                        "ssh_user": "student",
                        "private_key_path": str(key),
                        "known_hosts_path": str(hosts),
                    }
                },
            }
        )
    )
    monkeypatch.setattr(g["sys"], "argv", ["grade.py", str(context)])
    checks = g["CHECKS"]
    answers = {command: (output, rc) for _, command, output, rc in checks}

    def run(argv, **kwargs):
        assert kwargs["timeout"] <= 45
        command = argv[-1]
        if argv[0] == "/usr/bin/curl":
            assert "--noproxy" in argv and "--max-time" in argv
            command = command.replace("192.0.2.10", "{address}")
        else:
            assert "StrictHostKeyChecking=yes" in argv
            assert "ConnectTimeout=5" in argv
            assert f"UserKnownHostsFile={hosts}" in argv
        output, rc = answers[command]
        return SimpleNamespace(stdout=output + "\n", returncode=rc)

    monkeypatch.setattr(g["subprocess"], "run", run)
    assert g["main"]() == 0
    assert capsys.readouterr().out.count("PASS ") == len(checks)
    for _, command, output, rc in checks:
        for bad in [(output, rc + 1), (output + " stale", rc), ("", 255)]:
            answers[command] = bad
            assert g["main"]() == 1
            assert capsys.readouterr().out.count("FAIL ") == 1
        answers[command] = (output, rc)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("ssh", 45)

    monkeypatch.setattr(g["subprocess"], "run", timeout)
    assert g["main"]() == 1
    assert capsys.readouterr().out.count("FAIL ") == len(checks)


@pytest.mark.parametrize(
    "source,options,accepted",
    [
        ("/dev/vg210/records", "defaults", True),
        ("/dev/not-vg210-records", "defaults", False),
        ("/dev/root", "defaults", False),
        ("/dev/vg210/records", "ro", False),
        ("/dev/vg210/records", "noauto", False),
        ("/dev/vg210/records", "nofail", False),
    ],
)
@pytest.mark.parametrize("fstype", ["xfs", "ext4", "auto", "xfs\nxfs", ""])
def test_lx210_real_shell_rejects_lookalike_and_nonpersistent_mount(
    tmp_path, source, options, accepted, fstype
):
    """Run actual shell/awk predicates; only block-device discovery is shimmed."""
    accepted = accepted and fstype == "xfs"
    shim = tmp_path / "discovery"
    shim.write_text("""#!/usr/bin/python3
import os, sys
name = os.path.basename(sys.argv[0])
if name == "losetup": print("/dev/loop999")
elif name == "lvs": print("402653184")
elif name == "readlink": print(sys.argv[-1])
elif name == "findmnt":
    field = sys.argv[sys.argv.index("-nro") + 1]
    values = {"SOURCE": os.environ["SOURCE"], "TARGET": "/srv/lx210",
              "OPTIONS": os.environ["OPTIONS"], "FSTYPE": os.environ["FSTYPE"]}
    print(" ".join(values[f] for f in field.split(",")))
else: sys.exit(99)
""")
    shim.chmod(0o700)
    for command in ("losetup", "lvs", "readlink", "findmnt"):
        (tmp_path / command).symlink_to(shim)
    shell = shlex.split(grader("LX210")["CHECKS"][2][1])[-1]
    result = subprocess.run(
        ["/usr/bin/bash", "-euo", "pipefail", "-c", shell],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:/usr/bin",
            "SOURCE": source,
            "OPTIONS": options,
            "FSTYPE": fstype,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert (result.returncode == 0 and result.stdout.strip() == "ok") is accepted


@pytest.mark.parametrize("lab_id", IDS)
@pytest.mark.parametrize(
    "baseline,accepted",
    [
        (None, False),
        ("", False),
        ("not-a-boot-id", False),
        ("10000000-0000-0000-0000-000000000000", False),
        ("20000000-0000-0000-0000-000000000000", True),
    ],
)
def test_reboot_evidence_rejects_missing_or_malformed_baseline(
    tmp_path, lab_id, baseline, accepted
):
    current = tmp_path / "current"
    previous = tmp_path / "previous"
    current.write_text("10000000-0000-0000-0000-000000000000\n")
    if baseline is not None:
        previous.write_text(baseline + "\n")
    command = shlex.split(grader(lab_id)["CHECKS"][0][1])[-1]
    command = command.replace("/proc/sys/kernel/random/boot_id", str(current))
    command = command.replace(f"/var/lib/labctl/{lab_id.lower()}-boot", str(previous))
    result = subprocess.run(
        ["/usr/bin/bash", "-euo", "pipefail", "-c", command],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert (result.returncode == 0 and result.stdout.strip() == "ok") is accepted


def sandbox_setup(tmp_path, lab_id):
    """Execute setup twice in a path-rewritten sandbox, with privileged tools modeled.

    This checks staging/control flow, NOT real package, systemd, LVM or SELinux behavior.
    """
    guest = tmp_path / "guest"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for directory in (
        "etc/httpd/conf.d",
        "etc/systemd/system",
        "etc/selinux",
        "etc/default",
        "boot/loader/entries",
        "usr/local/sbin",
        "var/lib",
        "srv",
        "opt",
    ):
        (guest / directory).mkdir(parents=True, exist_ok=True)
    (guest / "etc/default/grub").write_text('GRUB_CMDLINE_LINUX="console=ttyS0"\n')
    (guest / "boot/loader/entries/kernel.conf").write_text("options console=ttyS0\n")
    (guest / "etc/fstab").touch()
    state = tmp_path / "services.json"
    state.write_text("{}")
    shim = binaries / "model"
    shim.write_text("""#!/usr/bin/python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
state = Path(os.environ["MODEL_STATE"])
data = json.loads(state.read_text())
if name in ("dnf", "chown", "chcon", "restorecon", "useradd", "id", "sleep"):
    sys.exit(0)
if name in ("mkfs.ext4", "mount", "umount", "grubby", "passwd",
            "grub2-editenv", "grub2-mkconfig"):
    sys.exit(0)
if name == "getent": print("root:!:0:0:99999:7:::"); sys.exit(0)
if name == "blkid": print("10000000-0000-0000-0000-000000000001"); sys.exit(0)
if name == "systemctl":
    if args[0] in ("enable", "disable"):
        for unit in args[1:]:
            if not unit.startswith("-"): data[unit] = args[0] == "enable"
        state.write_text(json.dumps(data))
    elif args[0] in ("is-enabled", "is-active"):
        yes = data.get(args[-1], False)
        print(("enabled" if args[0] == "is-enabled" else "active") if yes else "disabled")
        sys.exit(0 if yes else 1)
    elif "MainPID" in args: print(0)
    sys.exit(0)
if name == "ip": print("default via 192.0.2.1 dev eth0"); sys.exit(0)
if name == "nmcli": print("fixture"); sys.exit(0)
if name == "firewall-cmd":
    if any("query-service=cockpit" in a for a in args): print("yes"); sys.exit(0)
    if any("query-port=" in a for a in args): print("no"); sys.exit(1)
    if any("get-zone-of-interface" in a for a in args): print("public")
    sys.exit(0)
if name == "sha256sum": print("fixture checksum baseline"); sys.exit(0)
if name in ("findmnt", "losetup", "lvs", "pvs", "vgs", "semanage", "curl", "runuser"):
    sys.exit(1)
print("unexpected privileged command", name, args, file=sys.stderr)
sys.exit(97)
""")
    shim.chmod(0o700)
    for name in (
        "mkfs.ext4",
        "mount",
        "umount",
        "grubby",
        "grub2-editenv",
        "grub2-mkconfig",
        "passwd",
        "getent",
        "blkid",
        "dnf",
        "chown",
        "chcon",
        "restorecon",
        "useradd",
        "id",
        "sleep",
        "systemctl",
        "ip",
        "nmcli",
        "firewall-cmd",
        "sha256sum",
        "findmnt",
        "losetup",
        "lvs",
        "pvs",
        "vgs",
        "semanage",
        "curl",
        "runuser",
    ):
        (binaries / name).symlink_to(shim)

    def rewrite(text):
        for prefix in ("/usr/local", "/var/lib", "/etc", "/srv", "/opt", "/boot/"):
            text = text.replace(prefix, str(guest) + prefix)
        return text

    env = {**os.environ, "PATH": f"{binaries}:/usr/bin", "MODEL_STATE": str(state)}
    setup = rewrite((LABS / lab_id / "setup.sh").read_text())
    before = None
    for _ in range(2):
        result = subprocess.run(
            ["/usr/bin/bash", "-c", setup],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        after = {
            str(p.relative_to(guest)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in guest.rglob("*")
            if p.is_file()
        }
        if before is not None:
            assert after == before, "repeat setup must not restage or rewrite learner files"
        before = after
    if lab_id == "LX310":
        assert "UUID=00000000" in (guest / "etc/fstab").read_text()
        assert "systemd.unit=rescue.target" in (guest / "etc/default/grub").read_text()
        assert (guest / "var/lib/labctl/lx310-bls").read_text() == "kernel.conf\n"
    if lab_id == "LX311":
        assert "loop,ro" in (guest / "etc/fstab").read_text()
        assert "BIND=127.0.0.1" in (guest / "etc/lx311-export.conf").read_text()
        assert (guest / "srv/lx311/ledger.txt").read_text() == "LX311 retained ledger\n"
    return rewrite, env


@pytest.mark.parametrize("lab_id", IDS)
def test_each_local_objective_fails_on_sandboxed_pristine_setup(tmp_path, lab_id):
    rewrite, env = sandbox_setup(tmp_path, lab_id)
    for description, command, output, rc in grader(lab_id)["CHECKS"]:
        if not description.startswith("learner objective:") or command.startswith("http:"):
            continue
        shell = rewrite(shlex.split(command)[-1])
        result = subprocess.run(
            ["/usr/bin/bash", "-euo", "pipefail", "-c", shell],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert not (result.returncode == rc and result.stdout.strip() == output), description


@pytest.mark.parametrize("permissive,accepted", [(False, True), (True, False)])
def test_lx211_rejects_permissive_httpd_domain(tmp_path, permissive, accepted):
    """Execute the actual baseline command, modeling only SELinux utilities."""
    config = tmp_path / "config"
    config.write_text("SELINUX=enforcing\n")
    replies = {
        "getenforce": "Enforcing",
        "getsebool": "httpd_unified --> off",
        "semanage": "Customized Permissive Types\n\n" + ("httpd_t\n" if permissive else ""),
    }
    for name, reply in replies.items():
        path = tmp_path / name
        path.write_text("#!/bin/bash\nprintf '%s\\n' " + shlex.quote(reply) + "\n")
        path.chmod(0o700)
    shell = shlex.split(grader("LX211")["CHECKS"][1][1])[-1]
    shell = shell.replace("/etc/selinux/config", str(config))
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", shell],
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert (result.returncode == 0 and result.stdout.strip() == "ok") is accepted


@pytest.mark.parametrize(
    "mode,accepted",
    [
        ("correct", True),
        ("python", True),
        ("in_place", False),
        ("fixed", False),
        ("stale", False),
        ("hung", False),
        ("wrong_mode", False),
    ],
)
def test_lx212_fresh_transform_is_functional(tmp_path, mode, accepted):
    report = tmp_path / "reporter"
    script = """#!/bin/bash
set -euo pipefail
awk '$1=="ERROR" {e++} $1=="WARN" {w++} $1=="INFO" {i++}
END {printf "ERROR=%d\\nWARN=%d\\nINFO=%d\\n",e,w,i}' "$1" > "$2"
printf 'SHA256=%s\\n' "$(sha256sum "$1" | cut -d' ' -f1)" >> "$2"
"""
    if mode != "in_place":
        script = script.replace(
            "set -euo pipefail\n",
            'set -euo pipefail\nout=$2\nset -- "$1" "$(mktemp "$2.XXXXXX")"\n',
        )
        script += 'chmod 640 "$2"\nmv -f "$2" "$out"\n'
    else:
        script += 'chmod 640 "$2"\n'
    if mode == "python":
        script = """#!/usr/bin/python3
import collections, hashlib, os, sys, tempfile
from pathlib import Path
raw = Path(sys.argv[1]).read_bytes()
counts = collections.Counter(row.split()[0] for row in raw.splitlines() if row.split())
with tempfile.NamedTemporaryFile(dir=Path(sys.argv[2]).parent, delete=False) as out:
    out.write((''.join(f'{k}={counts[k.encode()]}\\n' for k in ('ERROR', 'WARN', 'INFO'))
               + 'SHA256=' + hashlib.sha256(raw).hexdigest() + '\\n').encode())
    os.fchmod(out.fileno(), 0o640)
os.replace(out.name, sys.argv[2])
"""
    if mode == "wrong_mode":
        script = script.replace("chmod 640", "chmod 644")
    if mode == "fixed":
        script = script.replace("e,w,i}", "3,2,1}")
    elif mode == "stale":
        script = "#!/bin/bash\nexit 0\n"
    elif mode == "hung":
        script = "#!/bin/bash\nsleep 8\n"
    report.write_text(script)
    report.chmod(0o700)
    shell = shlex.split(grader("LX212")["CHECKS"][1][1])[-1]
    shell = shell.replace("/var/lib/lx212", str(tmp_path))
    shell = shell.replace("/usr/local/sbin/lx212-report", str(report))
    # Only root identity is modeled; bytes, modes and rename/inode behavior are real.
    shell = shell.replace('"0:0 640"', f'"{os.getuid()}:{os.getgid()} 640"')
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", shell],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert (result.returncode == 0 and result.stdout.strip() == "ok") is accepted


@pytest.mark.parametrize("options,accepted", [("rw,relatime", True), ("ro,relatime", False)])
def test_lx210_runtime_mount_must_be_writable(tmp_path, options, accepted):
    shell = shlex.split(grader("LX210")["CHECKS"][1][1])[-1]
    outputs = {
        "findmnt": (
            '#!/bin/bash\ncase "$*" in *SOURCE*) echo /dev/vg210/records;; '
            "*FSTYPE*) echo xfs;; *OPTIONS*) echo " + shlex.quote(options) + ";; esac\n"
        ),
        "readlink": '#!/bin/bash\nprintf "%s\\n" "${@: -1}"\n',
        "losetup": "#!/bin/bash\necho /dev/loop999\n",
        "pvs": "#!/bin/bash\necho vg210\n",
        "vgs": "#!/bin/bash\necho 1\n",
    }
    for name, source in outputs.items():
        p = tmp_path / name
        p.write_text(source)
        p.chmod(0o700)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", shell],
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert (result.returncode == 0 and result.stdout.strip() == "ok") is accepted


@pytest.mark.parametrize("lab_id", ["LX310", "LX311"])
@pytest.mark.parametrize(
    "options,accepted",
    [
        ("loop,rw", True),
        ("loop,ro", False),
        ("loop,nofail", False),
        ("loop,noauto", False),
        ("defaults", False),
    ],
)
def test_recovery_persistent_mount_real_awk(tmp_path, lab_id, options, accepted):
    fstab = tmp_path / "fstab"
    name = lab_id.lower()
    fstab.write_text(f"/var/lib/labctl-media/{name}.img /srv/{name} ext4 {options} 0 2\n")
    check = next(c for d, c, _, _ in grader(lab_id)["CHECKS"] if "configured for every boot" in d)
    shell = shlex.split(check)[-1].replace("/etc/fstab", str(fstab))
    # Real awk predicate; findmnt verification needs actual mounted block devices.
    stub = tmp_path / "findmnt"
    stub.write_text("#!/bin/bash\nexit 0\n")
    stub.chmod(0o700)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", shell],
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert (result.returncode == 0 and result.stdout.strip() == "ok") is accepted
