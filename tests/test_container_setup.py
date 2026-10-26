"""Exercise setup control flow twice with all privileged commands sandboxed.

This proves script ordering/preservation, not guest Podman compatibility.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import test_container_labs as catalog

SHIM = """#!/usr/bin/python3
import json, os, subprocess, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ['CT_SANDBOX'])
with (root / 'calls').open('a') as out:
    out.write(json.dumps([name, *args]) + '\\n')
if name == 'dnf':
    # Model the managed guest image with full curl already installed.
    if 'curl-minimal' in args:
        print('curl-minimal conflicts with installed curl', file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(0)
if name == 'curl':
    raise SystemExit(0 if os.environ.get('CT_FAULT') == 'external-healthy' else 7)
if name == 'sleep':
    raise SystemExit(0)
if name in ('loginctl', 'systemctl', 'chown'):
    if os.environ.get('CT_LAB') == 'CT302':
        delegation = root / 'systemd/user@1000.service.d/50-labctl-cpu.conf'
        loaded = root / 'delegation-loaded'
        if name == 'systemctl' and args == ['daemon-reload']:
            assert delegation.read_text() == '[Service]\\nDelegate=cpu memory pids\\n'
            loaded.touch()
        if name == 'loginctl' or (name == 'systemctl' and args[0] == 'start'):
            assert loaded.exists(), 'CPU delegation must load before starting the user manager'
        assert not (name == 'systemctl' and args[0] in ('restart', 'stop')), (
            'Repeated setup must not interrupt an existing user manager'
        )
    raise SystemExit(0)
if name == 'id':
    print('1000')
elif name == 'install':
    clean = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in ('-o', '-g'):
            skip = True
        else:
            clean.append(arg)
    # Model root's install -d: implicit parents stay root-owned; only
    # explicit operands receive -o/-g. Real install still enforces modes.
    ownership_file = root / 'ownership.json'
    ownership = json.loads(ownership_file.read_text()) if ownership_file.exists() else {}
    owner = args[args.index('-o') + 1] if '-o' in args else 'root'
    group = args[args.index('-g') + 1] if '-g' in args else 'root'
    for arg in clean:
        if arg.startswith(str(root) + '/'):
            target = Path(arg)
            for parent in reversed([target, *target.parents]):
                if parent.is_relative_to(root) and not parent.exists():
                    ownership.setdefault(str(parent), ['root', 'root'])
            ownership[str(target)] = [owner, group]
    ownership_file.write_text(json.dumps(ownership))
    os.execv('/usr/bin/install', ['/usr/bin/install', *clean])
elif name == 'runuser':
    assert args[:3] == ['-u', 'student', '--']
    raise SystemExit(subprocess.run(args[3:]).returncode)
elif name == 'usermod':
    lo, hi = map(int, args[1].split('-'))
    target = root / ('subuid' if args[0] == '--add-subuids' else 'subgid')
    with target.open('a') as out:
        out.write(f'student:{lo}:{hi-lo+1}\\n')
elif name == 'podman':
    ownership = json.loads((root / 'ownership.json').read_text())
    for relative in ('student/.config', 'student/.config/containers'):
        config = root / relative
        assert ownership[str(config)] == ['student', 'student'], (
            f'Podman configuration path {relative} is not student-owned'
        )
        assert config.stat().st_mode & 0o777 == 0o700
    marker = root / 'image'
    if args[:2] == ['image', 'exists']:
        raise SystemExit(0 if marker.exists() else 1)
    if args[:2] == ['image', 'inspect']:
        print('sha256:fixture ["registry.example/image@sha256:fixture"]')
    elif args[0] == 'pull':
        assert os.environ['HOME'] == str(root / 'student')
        assert os.environ['XDG_RUNTIME_DIR'] == str(root / 'run' / '1000')
        marker.touch()
    elif args[:2] == ['volume', 'exists']:
        raise SystemExit(0 if (root / 'volume').exists() else 1)
    elif args[:2] == ['volume', 'create']:
        (root / 'volume').touch()
    elif args[:2] == ['volume', 'inspect']:
        assert (root / 'volume').exists()
        print(json.dumps([{'Name': 'ct402-data', 'CreatedAt': 'fixture-original'}]))
    elif args[:2] == ['container', 'exists']:
        raise SystemExit(0 if (root / 'container').exists() else 1)
    elif args[0] == 'exec':
        if os.environ.get('CT_FAULT') == 'internal-broken':
            raise SystemExit(1)
        assert 'http://127.0.0.1:9090/health' in args
        print('CT402 recovered API')
    elif args[0] == 'run':
        assert '--pull=never' in args
        if '--name' in args:
            (root / 'container').touch()
    else:
        raise RuntimeError('unexpected Podman operation ' + repr(args))
else:
    raise RuntimeError('unexpected shim ' + name)
"""


@pytest.mark.parametrize(
    ("lab_id", "fault"),
    [(lab_id, "none") for lab_id in catalog.CT_IDS]
    + [("CT402", "internal-broken"), ("CT402", "external-healthy")],
)
def test_setup_twice_preserves_work_and_preloads_once(
    tmp_path: Path, lab_id: str, fault: str
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for command in (
        "dnf",
        "loginctl",
        "systemctl",
        "chown",
        "id",
        "install",
        "runuser",
        "usermod",
        "podman",
        "curl",
        "sleep",
    ):
        shim = bindir / command
        shim.write_text(SHIM)
        shim.chmod(0o700)
    (tmp_path / "student").mkdir()
    for filename in ("subuid", "subgid"):
        (tmp_path / filename).write_text("other:100000:65536\n")
    (tmp_path / "image").touch()  # Cached tags must still refresh on fresh setup.
    source = (catalog.LABS / lab_id / "setup.sh").read_text()
    for original, local in {
        "/home/student": str(tmp_path / "student"),
        "/run/user": str(tmp_path / "run"),
        "/var/lib/labctl": str(tmp_path / "state"),
        "/etc/systemd/system": str(tmp_path / "systemd"),
        "/etc/subuid": str(tmp_path / "subuid"),
        "/etc/subgid": str(tmp_path / "subgid"),
        "/usr/sbin/usermod": str(bindir / "usermod"),
        "/usr/bin/podman": str(bindir / "podman"),
    }.items():
        source = source.replace(original, local)
    script = tmp_path / "setup.sh"
    script.write_text(source)
    env = {
        **os.environ,
        "PATH": f"{bindir}:/usr/bin:/bin",
        "CT_SANDBOX": str(tmp_path),
        "CT_FAULT": fault,
        "CT_LAB": lab_id,
    }
    for iteration in range(2):
        result = subprocess.run(
            ["/usr/bin/bash", script], env=env, capture_output=True, text=True, check=False
        )
        if fault != "none":
            assert result.returncode != 0
            assert not (tmp_path / "state/ct402-initialized").exists()
            assert not (tmp_path / "state/ct402-fault-baseline").exists()
            return
        assert result.returncode == 0, result.stderr
        guide = tmp_path / "student/LAB.md"
        if iteration == 0:
            assert not guide.exists(), "Setup must leave guide publication to cloud-init"
            guide.write_text("learner notes retained\n")
        else:
            assert guide.read_text() == "learner notes retained\n"
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    installs = [call for call in calls if call[:3] == ["dnf", "-y", "install"]]
    assert len(installs) == 2
    assert all("curl" in call and "curl-minimal" not in call for call in installs)
    assert sum(call[:2] == ["podman", "pull"] for call in calls) == (
        2 if lab_id in {"CT202", "CT301", "CT402"} else 1
    )
    assert "sha256:fixture" in (tmp_path / "state/ct-image-provenance").read_text()
    assert sum(call[0] == "usermod" for call in calls) == 2
    runs = [call for call in calls if call[:2] == ["podman", "run"]]
    assert len(runs) == (2 if lab_id == "CT402" else 0)
    if lab_id == "CT402":
        assert any(call[:3] == ["podman", "exec", "ct402-api"] for call in calls)
        assert any(call[0] == "curl" for call in calls)
        assert "/usr/sbin/httpd" in runs[-1]
        assert "registry.access.redhat.com/ubi9/ubi:9.8" in runs[0]
        assert "registry.access.redhat.com/ubi9/httpd-24:9.8" in runs[1]
