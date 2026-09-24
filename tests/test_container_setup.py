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
if name in ('loginctl', 'systemctl', 'chown'):
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
    if args[0] == 'pull':
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
    elif args[0] == 'run':
        assert '--pull=never' in args
        if '--name' in args:
            (root / 'container').touch()
    else:
        raise RuntimeError('unexpected Podman operation ' + repr(args))
else:
    raise RuntimeError('unexpected shim ' + name)
"""


@pytest.mark.parametrize("lab_id", catalog.CT_IDS)
def test_setup_twice_preserves_work_and_preloads_once(tmp_path: Path, lab_id: str) -> None:
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
    ):
        shim = bindir / command
        shim.write_text(SHIM)
        shim.chmod(0o700)
    (tmp_path / "student").mkdir()
    for filename in ("subuid", "subgid"):
        (tmp_path / filename).write_text("other:100000:65536\n")
    source = (catalog.LABS / lab_id / "setup.sh").read_text()
    for original, local in {
        "/home/student": str(tmp_path / "student"),
        "/run/user": str(tmp_path / "run"),
        "/var/lib/labctl": str(tmp_path / "state"),
        "/etc/subuid": str(tmp_path / "subuid"),
        "/etc/subgid": str(tmp_path / "subgid"),
        "/usr/sbin/usermod": str(bindir / "usermod"),
        "/usr/bin/podman": str(bindir / "podman"),
    }.items():
        source = source.replace(original, local)
    script = tmp_path / "setup.sh"
    script.write_text(source)
    env = {**os.environ, "PATH": f"{bindir}:/usr/bin:/bin", "CT_SANDBOX": str(tmp_path)}
    for iteration in range(2):
        result = subprocess.run(
            ["/usr/bin/bash", script], env=env, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        guide = tmp_path / "student/LAB.md"
        if iteration == 0:
            assert lab_id in guide.read_text()
            guide.write_text("learner notes retained\n")
        else:
            assert guide.read_text() == "learner notes retained\n"
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    installs = [call for call in calls if call[:3] == ["dnf", "-y", "install"]]
    assert len(installs) == 2
    assert all("curl" in call and "curl-minimal" not in call for call in installs)
    assert sum(call[:2] == ["podman", "pull"] for call in calls) == 1
    assert sum(call[0] == "usermod" for call in calls) == 2
    runs = [call for call in calls if call[:2] == ["podman", "run"]]
    assert len(runs) == (2 if lab_id == "CT402" else 0)
