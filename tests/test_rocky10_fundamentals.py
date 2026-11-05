# ruff: noqa: E501, S102
"""Rocky 10 entry-level contracts and bounded pinned-SSH grading tests."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from labctl.definitions import load_definition


@pytest.mark.parametrize("producer_failure", [False, True])
def test_lx112_drains_ip_output_without_hiding_producer_failure(tmp_path, producer_failure):
    """grep -q can SIGPIPE a successful chunked ip producer under pipefail."""
    nmcli = tmp_path / "nmcli"
    nmcli.write_text("#!/bin/sh\nprintf 'lx112-dummy\\n'\n")
    nmcli.chmod(0o700)
    ip = tmp_path / "ip"
    ip.write_text(
        "#!/usr/bin/python3\nimport os, signal, sys\n"
        "signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
        "if '-d' in sys.argv: line='dummy\\n'\n"
        "elif '-4' in sys.argv: line='inet 192.0.2.20/24 scope global\\n'\n"
        "else: line='inet6 2001:db8:112::20/64 scope global\\n'\n"
        "os.write(1,line.encode())\n"
        "for _ in range(64): os.write(1,b'additional diagnostic data\\n'*4096)\n"
        f"sys.exit({1 if producer_failure else 0})\n"
    )
    ip.chmod(0o700)
    command = module("LX112")["CHECKS"]["active dummy and both addresses"][0]
    shell = shlex.split(command)[-1]
    result = subprocess.run(
        ["/usr/bin/bash", "-o", "pipefail", "-c", shell],
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert (result.returncode == 0 and result.stdout == "ok\n") is not producer_failure


LABS = Path(__file__).parents[1] / "src/labctl/data/labs"
IDS = ["LX010", "LX011", "LX012", "LX110", "LX111", "LX112"]

SOLUTIONS = {
    "LX010": """mkdir -p /home/student/lx010/{notes,inbox}
cp /srv/lx010/welcome.txt /home/student/lx010/notes/welcome.txt
printf 'ready\\n' > /home/student/lx010/.ready
(cd /home/student/lx010/notes && pwd > ../location.txt)
""",
    "LX011": """mkdir -p /home/student/lx011
grep '^ERROR ' /srv/lx011/events.log > /home/student/lx011/errors.txt
cut -d' ' -f1 /srv/lx011/events.log | LC_ALL=C sort -u > /home/student/lx011/levels.txt
awk '{n[$1]++} END {for (l in n) print l,n[l]}' /srv/lx011/events.log | LC_ALL=C sort > /home/student/lx011/counts.txt
awk '{print NR \":\" $0}' /srv/lx011/events.log > /home/student/lx011/numbered.txt
""",
    "LX012": """mkdir -p /home/student/lx012
printf 'Rocky archive practice\\n' > /home/student/lx012/message.txt
chmod 0640 /home/student/lx012/message.txt
ln /home/student/lx012/message.txt /home/student/lx012/message.hard
ln -s message.txt /home/student/lx012/current
tar -czf /home/student/lx012/backup.tar.gz -C /home/student/lx012 message.txt
""",
}

BREAKS = {
    "LX010": [
        "rmdir /home/student/lx010/inbox",
        "rm /home/student/lx010/notes/welcome.txt; ln -s /srv/lx010/welcome.txt /home/student/lx010/notes/welcome.txt",
        "printf wrong > /home/student/lx010/.ready",
        "printf relative > /home/student/lx010/location.txt",
    ],
    "LX011": [
        f"printf 'extra\\n' >> /home/student/lx011/{name}.txt"
        for name in ["errors", "levels", "counts", "numbered"]
    ],
    "LX012": [
        "chmod 0666 /home/student/lx012/message.txt",
        "rm /home/student/lx012/message.hard; cp /home/student/lx012/message.txt /home/student/lx012/message.hard",
        "rm /home/student/lx012/current; cp /home/student/lx012/message.txt /home/student/lx012/current",
        "tar -cf /home/student/lx012/backup.tar.gz -C /home/student/lx012 message.txt",
    ],
}


@pytest.mark.parametrize("lab_id", list(SOLUTIONS))
@pytest.mark.parametrize("bad_index", range(4))
@pytest.mark.parametrize("shortcut", [False, True])
def test_real_file_predicates_and_idempotent_setup(lab_id, bad_index, shortcut, tmp_path):
    import grp
    import pwd

    owner = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name

    def mapped(text):
        return (
            text.replace("/home/student", str(tmp_path / "home"))
            .replace("/srv/", str(tmp_path / "srv") + "/")
            .replace("/var/lib/labctl", str(tmp_path / "state"))
            .replace("-o student -g student ", "")
            .replace("student:student 640", f"{owner}:{group} 640")
        )

    def run(text):
        return subprocess.run(
            ["/usr/bin/bash", "-o", "pipefail", "-c", mapped(text)],
            text=True,
            capture_output=True,
            timeout=5,
        )

    setup = (LABS / lab_id / "setup.sh").read_text()
    assert run(setup).returncode == 0
    assert run(setup).returncode == 0
    checks = [shlex.split(row[0])[-1] for row in module(lab_id)["CHECKS"].values()]
    assert all(run(command).returncode != 0 for command in checks)
    result = run("set -e\n" + SOLUTIONS[lab_id])
    assert result.returncode == 0, result.stderr
    for command in checks:
        result = run(command)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "ok\n"
    assert run(setup).returncode == 0
    assert all(run(command).returncode == 0 for command in checks)
    if shortcut and not (lab_id == "LX012" and bad_index == 2):
        paths = {
            "LX010": ["inbox", "notes/welcome.txt", ".ready", "location.txt"],
            "LX011": ["errors.txt", "levels.txt", "counts.txt", "numbered.txt"],
            "LX012": ["message.txt", "message.hard", "current", "backup.tar.gz"],
        }
        path = tmp_path / "home" / lab_id.lower() / paths[lab_id][bad_index]
        backup = path.with_name(path.name + ".real")
        path.rename(backup)
        path.symlink_to(backup)
    else:
        result = run(BREAKS[lab_id][bad_index])
        assert result.returncode == 0, result.stderr
    assert run(checks[bad_index]).returncode != 0


@pytest.mark.parametrize(
    ("lab_id", "adversarial"),
    [(lab, "none") for lab in ["LX110", "LX111", "LX112"]]
    + [("LX110", case) for case in ["primary-team", "wheel", "broad-sudo", "sudo-error"]]
    + [("LX112", case) for case in ["duplicate-nm", "colon-filename"]]
    + [("LX110", case) for case in ["safe-sudo-tags", "numeric-root", "extra-sudo-command"]]
    + [("LX111", "wrong-boot-target")],
)
def test_level1_emitted_commands_with_semantic_fixtures(lab_id, tmp_path, adversarial):
    """Execute real Bash/awk pipelines; shim only guest administration tools."""
    answers = {}
    mutations = []
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fixture = tmp_path / "answers.json"
    mapping = {
        "/home/lxoperator": str(tmp_path / "operator"),
        "/srv/lx110-share": str(tmp_path / "share"),
        "/usr/bin/tree": str(tmp_path / "tree"),
        "/home/student/lx111-journal.txt": str(tmp_path / "journal.txt"),
    }
    (tmp_path / "operator").mkdir()
    (tmp_path / "share").mkdir()
    (tmp_path / "tree").touch(mode=0o755)
    (tmp_path / "journal.txt").write_text("LX111 service ready\n")

    def mapped(text):
        for source, destination in mapping.items():
            text = text.replace(source, destination)
        return text

    def answer(command, output, status=0):
        parts = shlex.split(mapped(command))
        key = json.dumps(parts)
        answers[key] = [status, mapped(output)]
        return key

    if lab_id == "LX110":
        mutations.append(
            answer(
                "getent passwd lxoperator", "lxoperator:x:1001:1001::/home/lxoperator:/bin/bash\n"
            )
        )
        # EL10 shadow-utils passwd uses L, unlike legacy Red Hat passwd's LK.
        answer("passwd -S lxoperator", "lxoperator L 2026-01-01 0 45 7 -1\n")
        mutations.append(answer("id -nG lxoperator", "lxoperator lxteam\n"))
        answer("getent group lxteam", "lxteam:x:1002:lxoperator\n")
        answer("id -gn lxoperator", "lxoperator\n")
        answer(
            "sudo -n -l -U lxoperator",
            "User lxoperator may run the following commands on node:\n    (root) NOPASSWD: /usr/bin/id -u\n",
        )
        mutations.append(answer("getent shadow lxoperator", "lxoperator:!:20000:0:45:7:::\n"))
        mutations.append(answer("stat -c '%U:%G %a' /srv/lx110-share", "root:lxteam 2770\n"))
        answer("visudo -c", "parsed OK\n")
        mutations.append(answer("runuser -u lxoperator -- sudo -n -u root /usr/bin/id -u", "0\n"))
        answer("sudo -n -l -U lxoperator -u root /usr/bin/cat /etc/shadow", "", 1)
    elif lab_id == "LX111":
        mutations.append(answer("rpm -q tree", "tree-2.1\n"))
        mutations.append(answer("systemctl is-enabled lx111-ready.service", "enabled\n"))
        answer(
            "systemctl show multi-user.target -p Wants -p Requires --value",
            "sshd.service lx111-ready.service\n",
        )
        mutations.append(answer("systemctl is-active lx111-ready.service", "active\n"))
        answer("systemctl show lx111-ready.service -p ExecMainStatus --value", "0\n")
        answer(
            "systemctl show lx111-ready.service -p ExecStart --value",
            "{ path=/usr/bin/echo ; argv[]=/usr/bin/echo LX111 service ready ; }\n",
        )
        mutations.append(
            answer(
                "journalctl -b -u lx111-ready.service -o cat --no-pager", "LX111 service ready\n"
            )
        )
        mutations.append(None)
    else:
        mutations.append(answer("hostnamectl --static", "workshop.lab\n"))
        answer("hostname", "workshop.lab\n")
        for field, value in {
            "connection.type": "dummy",
            "connection.interface-name": "labdummy0",
            "connection.autoconnect": "yes",
            "ipv4.method": "manual",
            "ipv4.addresses": "192.0.2.20/24",
            "ipv4.never-default": "yes",
            "ipv4.gateway": "",
            "ipv6.method": "manual",
            "ipv6.addresses": "2001:db8:112::20/64",
            "ipv6.never-default": "yes",
            "ipv6.gateway": "",
        }.items():
            key = answer(f"nmcli --escape no -g {field} connection show lx112-dummy", value + "\n")
            answer(
                f"nmcli -g {field} connection show lx112-dummy", value.replace(":", "\\:") + "\n"
            )
            if field in {"connection.type", "ipv4.addresses", "ipv6.addresses"}:
                mutations.append(key)
        answer(
            "nmcli -t -f NAME,FILENAME connection show",
            "lx112-dummy:/etc/NetworkManager/system-connections/lx112-dummy.nmconnection\n",
        )
        mutations.append(
            answer("nmcli --escape no -g GENERAL.CONNECTION device show labdummy0", "lx112-dummy\n")
        )
        answer("nmcli -g GENERAL.CONNECTION device show labdummy0", "lx112-dummy\n")
        answer("ip -d link show dev labdummy0", "dummy\n")
        answer(
            "ip -o -4 address show dev labdummy0", "3: labdummy0 inet 192.0.2.20/24 scope global\n"
        )
        answer(
            "ip -o -6 address show dev labdummy0",
            "3: labdummy0 inet6 2001:db8:112::20/64 scope global\n",
        )
    shim = """#!/usr/bin/python3
import json, os, sys
from pathlib import Path
key = json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]])
answers = json.loads(Path(os.environ['ANSWERS']).read_text())
status, output = answers.get(key, [127, 'unconfigured guest command'])
print(output, end='')
sys.exit(status)
"""
    for name in [
        "getent",
        "passwd",
        "id",
        "stat",
        "visudo",
        "runuser",
        "sudo",
        "rpm",
        "systemctl",
        "journalctl",
        "hostnamectl",
        "hostname",
        "nmcli",
        "ip",
    ]:
        path = bin_dir / name
        path.write_text(shim)
        path.chmod(0o700)
    checks = [mapped(shlex.split(row[0])[-1]) for row in module(lab_id)["CHECKS"].values()]

    def run(command):
        return subprocess.run(
            ["/usr/bin/bash", "-o", "pipefail", "-c", command],
            env={"PATH": f"{bin_dir}:/usr/bin", "ANSWERS": str(fixture)},
            text=True,
            capture_output=True,
            timeout=5,
        )

    fixture.write_text("{}")
    (tmp_path / "journal.txt").write_text("not yet exported\n")
    assert all(run(command).returncode != 0 for command in checks)
    (tmp_path / "journal.txt").write_text("LX111 service ready\n")
    if lab_id == "LX110" and adversarial in {"primary-team", "wheel", "broad-sudo", "sudo-error"}:
        index = 1 if adversarial == "primary-team" else 4
        if adversarial == "primary-team":
            answer("id -gn lxoperator", "lxteam\n")
        elif adversarial == "wheel":
            answer("id -nG lxoperator", "lxoperator lxteam wheel\n")
        elif adversarial == "broad-sudo":
            answer(
                "sudo -n -l -U lxoperator",
                "User lxoperator may run the following commands on node:\n    (root) NOPASSWD: ALL, !/usr/bin/cat /etc/shadow\n",
            )
        else:
            answer("sudo -n -l -U lxoperator -u root /usr/bin/cat /etc/shadow", "", 127)
        fixture.write_text(json.dumps(answers))
        assert run(checks[index]).returncode != 0
        return
    if adversarial in {"safe-sudo-tags", "numeric-root", "extra-sudo-command"}:
        rules = {
            "safe-sudo-tags": "(root : root) NOEXEC: NOPASSWD: /usr/bin/id -u",
            "numeric-root": "(#0) NOPASSWD: /usr/bin/id -u",
            "extra-sudo-command": "(root) NOPASSWD: /usr/bin/id -u, /usr/bin/bash",
        }
        answer(
            "sudo -n -l -U lxoperator",
            "User lxoperator may run the following commands on node:\n    "
            + rules[adversarial]
            + "\n",
        )
        fixture.write_text(json.dumps(answers))
        assert (run(checks[4]).returncode == 0) == (adversarial != "extra-sudo-command")
        return
    if adversarial == "wrong-boot-target":
        answer("systemctl show multi-user.target -p Wants -p Requires --value", "sshd.service\n")
        fixture.write_text(json.dumps(answers))
        assert run(checks[1]).returncode != 0
        return
    if lab_id == "LX112" and adversarial in {"duplicate-nm", "colon-filename"}:
        row = "lx112-dummy:/etc/NetworkManager/system-connections/lx112-dummy.nmconnection\n"
        output = (
            row * 2
            if adversarial == "duplicate-nm"
            else row.replace("dummy.nmconnection", "dummy\\:custom.nmconnection")
        )
        answer("nmcli -t -f NAME,FILENAME connection show", output)
        fixture.write_text(json.dumps(answers))
        result = run(checks[1])
        assert (result.returncode == 0) == (adversarial == "colon-filename"), result.stderr
        return
    fixture.write_text(json.dumps(answers))
    for command in checks:
        result = run(command)
        assert result.returncode == 0, (command, result.stderr)
    if lab_id == "LX112":
        for index, family in [(2, "ipv4"), (3, "ipv6")]:
            broken = dict(answers)
            key = json.dumps(
                shlex.split(f"nmcli --escape no -g {family}.gateway connection show lx112-dummy")
            )
            broken[key] = [10, ""]
            fixture.write_text(json.dumps(broken))
            assert run(checks[index]).returncode != 0
    for index, key in enumerate(mutations):
        broken = dict(answers)
        if key is None:
            (tmp_path / "journal.txt").write_text("wrong\n")
        else:
            # Membership must fail even when getent still names the member.
            broken[key] = [0, "wrong\n"] if lab_id != "LX111" or index != 0 else [1, "tree\n"]
        fixture.write_text(json.dumps(broken))
        assert run(checks[index]).returncode != 0, checks[index]


@pytest.mark.parametrize("lab_id", ["LX110", "LX111", "LX112"])
def test_level1_setup_preserves_work_on_repeat(lab_id, tmp_path):
    """No privileged host operation: all guest paths and tools are sandboxed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    for tool in ("dnf", "systemctl"):
        path = bin_dir / tool
        path.write_text('#!/usr/bin/bash\nprintf "%s %s\\n" "${0##*/}" "$*" >> "$CALLS"\n')
        path.chmod(0o700)
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    setup = (LABS / lab_id / "setup.sh").read_text()
    setup = (
        setup.replace("/var/lib/labctl", str(tmp_path / "state"))
        .replace("/home/student", str(tmp_path / "home"))
        .replace("/etc/systemd/system", str(unit_dir))
        .replace("-o student -g student ", "")
    )
    env = {"PATH": f"{bin_dir}:/usr/bin", "CALLS": str(log)}
    result = subprocess.run(["/usr/bin/bash", "-c", setup], env=env, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    calls = log.read_text() if log.exists() else ""
    unit = unit_dir / "lx111-ready.service"
    if lab_id == "LX111":
        assert "dnf -y remove tree" in calls
        assert "systemctl disable --now lx111-ready.service" in calls
        assert "enable" not in calls and "start" not in calls
        unit = unit_dir / "lx111-ready.service"
        assert "ExecStart=/usr/bin/echo LX111 service ready" in unit.read_text()
        unit.write_text("learner changes\n")
    elif lab_id == "LX112":
        assert "nmcli" not in setup
        assert "hostnamectl" not in setup
        assert "labdummy0" not in setup
    sentinel = tmp_path / "home" / "learner-work"
    sentinel.write_text("keep\n")
    result = subprocess.run(["/usr/bin/bash", "-c", setup], env=env, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert sentinel.read_text() == "keep\n"
    assert (log.read_text() if log.exists() else "") == calls
    if lab_id == "LX111":
        assert unit.read_text() == "learner changes\n"


def test_symlink_to_message_hardlink_is_same_file(tmp_path):
    message = tmp_path / "message.txt"
    message.write_text("Rocky archive practice\n")
    (tmp_path / "message.hard").hardlink_to(message)
    (tmp_path / "current").symlink_to("message.hard")
    command = shlex.split(module("LX012")["CHECKS"]["symbolic link target"][0])[-1]
    command = command.replace("/home/student/lx012", str(tmp_path))
    result = subprocess.run(["/usr/bin/bash", "-c", command], capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    (tmp_path / "message.hard").unlink()
    (tmp_path / "message.hard").write_bytes(message.read_bytes())
    result = subprocess.run(["/usr/bin/bash", "-c", command], capture_output=True, timeout=5)
    assert result.returncode != 0


@pytest.mark.parametrize(
    "fault", ["none", "bytes", "mode", "prefix", "symlink", "hardlink", "duplicate", "extra"]
)
def test_archive_member_identity_and_bytes(tmp_path, fault):
    import io
    import tarfile

    archive = tmp_path / "backup.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        entry = tarfile.TarInfo("dir/message.txt" if fault == "prefix" else "message.txt")
        entry.mode = 0o644 if fault == "mode" else 0o640
        data = b"wrong\n" if fault == "bytes" else b"Rocky archive practice\n"
        entry.size = len(data)
        if fault in {"symlink", "hardlink"}:
            entry.type = tarfile.SYMTYPE if fault == "symlink" else tarfile.LNKTYPE
            entry.linkname = "other.txt"
            entry.size = 0
        tar.addfile(entry, io.BytesIO(data))
        if fault in {"duplicate", "extra"}:
            if fault == "extra":
                entry.name = "extra.txt"
            tar.addfile(entry, io.BytesIO(data))
    command = shlex.split(module("LX012")["CHECKS"]["gzip archive contents and mode"][0])[-1]
    command = command.replace("/home/student/lx012", str(tmp_path))
    result = subprocess.run(["/usr/bin/bash", "-c", command], capture_output=True, timeout=5)
    assert (result.returncode == 0) == (fault == "none"), result.stderr


def module(lab_id: str) -> dict:
    path = LABS / lab_id / "grade.py"
    assert path.is_file(), "standalone grader is required"
    namespace = {"__name__": "fixture"}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    return namespace


@pytest.mark.parametrize("lab_id", IDS)
def test_definition_and_transport(lab_id, tmp_path, monkeypatch, capsys):
    definition = load_definition(LABS / lab_id / "lab.yaml")
    assert definition.id == lab_id
    assert len(definition.vms) == 1
    assert definition.vms[0].image == "rocky:10"
    assert definition.vms[0].ssh_user == "student"
    assert "Starting state" in definition.instructions
    assert "Required outcomes" in definition.instructions
    assert "Forbidden shortcuts" in definition.instructions
    assert f"labctl grade {lab_id}" in definition.instructions
    assert os.access(definition.grader, os.X_OK)
    assert os.access(definition.vms[0].setup, os.X_OK)
    ns = module(lab_id)
    assert len(ns["CHECKS"]) >= 4
    key, known = tmp_path / "key", tmp_path / "known_hosts"
    key.touch()
    known.touch()
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
                        "known_hosts_path": str(known),
                    }
                },
            }
        )
    )
    monkeypatch.setattr(ns["sys"], "argv", ["grade.py", str(context)])
    commands = [row[0] for row in ns["CHECKS"].values()]
    for bad_index in range(-1, len(commands)):
        for fault in ("status", "output", "timeout"):
            calls = []

            def fake_run(argv, calls=calls, bad_index=bad_index, fault=fault, **kwargs):
                assert kwargs["timeout"] <= 30
                assert "StrictHostKeyChecking=yes" in argv
                assert f"UserKnownHostsFile={known}" in argv
                assert "BatchMode=yes" in argv
                assert str(key) in argv
                assert argv[-2] == "student@192.0.2.10"
                index = len(calls)
                calls.append(argv)
                if index == bad_index and fault == "timeout":
                    raise subprocess.TimeoutExpired(argv, 30)
                return SimpleNamespace(
                    returncode=1 if index == bad_index and fault == "status" else 0,
                    stdout="wrong" if index == bad_index and fault == "output" else "ok\n",
                )

            with monkeypatch.context() as patch:
                patch.setattr(ns["subprocess"], "run", fake_run)
                assert ns["main"]() == (0 if bad_index == -1 else 1)
            lines = capsys.readouterr().out.splitlines()
            assert len(lines) == len(commands)
            assert sum(line.startswith("FAIL ") for line in lines) == (bad_index != -1)
            assert len(calls) == len(commands)
    context.write_text(json.dumps({"schema_version": 1, "lab_id": "wrong"}))
    assert ns["main"]() == 2


@pytest.mark.parametrize("lab_id", IDS)
def test_emitted_shell_syntax(lab_id):
    ns = module(lab_id)
    setup = LABS / lab_id / "setup.sh"
    result = subprocess.run(["/usr/bin/bash", "-n", str(setup)], capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    for command, expected in ns["CHECKS"].values():
        assert expected == "ok"
        parts = shlex.split(command)
        assert parts[-2] == "-c"
        result = subprocess.run(
            ["/usr/bin/bash", "-n", "-c", parts[-1]], capture_output=True, timeout=5
        )
        assert result.returncode == 0, result.stderr
