"""LX310 setup execution with privileged commands modeled, not guest boot proof."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

SETUP = Path(__file__).parents[1] / "src/labctl/data/labs/LX310/setup.sh"


@pytest.mark.parametrize(
    "console,speed", [("ttyS0,115200n8", "115200"), ("ttyS0,9600n8", "9600"), ("ttyS0", "115200")]
)
@pytest.mark.parametrize("cmdline_state", ["populated", "empty", "missing"])
def test_setup_exposes_serial_grub_without_solving_faults(tmp_path, console, speed, cmdline_state):
    guest = tmp_path / "guest"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for directory in ("etc/default", "boot/grub2", "boot/loader/entries", "var/lib", "srv"):
        (guest / directory).mkdir(parents=True)
    platform = (
        f"root=UUID=platform ro console=tty0 console={console} "
        "rd.lvm.lv=rl/root crashkernel=2G-64G:256M"
    )
    defaults = guest / "etc/default/grub"
    default_args = platform if cmdline_state == "populated" else ""
    cmdline_assignment = (
        "" if cmdline_state == "missing" else f'GRUB_CMDLINE_LINUX="{default_args}"\n'
    )
    defaults.write_text(
        cmdline_assignment + "GRUB_ENABLE_BLSCFG=true\n"
        'GRUB_TERMINAL="gfxterm"\nGRUB_TERMINAL_INPUT="console"\n'
        'GRUB_TERMINAL_OUTPUT="gfxterm"\nGRUB_TIMEOUT=0\nGRUB_TIMEOUT_STYLE=hidden\n'
    )
    (guest / "etc/fstab").write_text("# platform mount\n")
    entry = guest / "boot/loader/entries/kernel.conf"
    entry.write_text(f"options {platform}\n")
    log = tmp_path / "commands"
    shim = binaries / "model"
    shim.write_text("""#!/usr/bin/python3
import os, pathlib, subprocess, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['COMMAND_LOG'], 'a') as f:
    f.write(name + ' ' + ' '.join(args) + '\\n')
if name == 'getent': print('root:!:0:0:99999:7:::')
if name == 'blkid': print('fixture-uuid')
if name == 'grubby':
    p = pathlib.Path(os.environ['ENTRY'])
    p.write_text(p.read_text().rstrip() + ' systemd.unit=rescue.target\\n')
if name == 'grub2-mkconfig':
    assert args == ['-o', os.environ['GRUB_CONFIG']]
    # Capture effective shell variables exactly when generation is invoked.
    fields = ('GRUB_TERMINAL', 'GRUB_TERMINAL_INPUT', 'GRUB_TERMINAL_OUTPUT',
              'GRUB_SERIAL_COMMAND', 'GRUB_TIMEOUT_STYLE', 'GRUB_TIMEOUT',
              'GRUB_CMDLINE_LINUX', 'GRUB_ENABLE_BLSCFG')
    command = '. "$1"; printf "%s\\n" ' + ' '.join('"${' + f + '-}"' for f in fields)
    result = subprocess.run(['/usr/bin/bash', '-c', command,
                             'fixture', os.environ['DEFAULTS']],
                            capture_output=True, text=True, check=True)
    pathlib.Path(args[1]).write_text(result.stdout)
""")
    shim.chmod(0o700)
    for name in (
        "dnf",
        "mkfs.ext4",
        "mount",
        "umount",
        "grubby",
        "passwd",
        "getent",
        "blkid",
        "systemctl",
        "grub2-mkconfig",
        "grub2-editenv",
    ):
        (binaries / name).symlink_to(shim)
    setup = SETUP.read_text()
    for prefix in ("/var/lib", "/etc", "/srv", "/boot/"):
        setup = setup.replace(prefix, str(guest) + prefix)
    env = {
        **os.environ,
        "PATH": f"{binaries}:/usr/bin",
        "COMMAND_LOG": str(log),
        "ENTRY": str(entry),
        "DEFAULTS": str(defaults),
        "GRUB_CONFIG": str(guest / "boot/grub2/grub.cfg"),
    }
    # An inherited value must not conceal the Rocky image's absent assignment.
    env.pop("GRUB_CMDLINE_LINUX", None)
    if cmdline_state == "missing":
        assert "GRUB_CMDLINE_LINUX" not in defaults.read_text()
    result = subprocess.run(
        ["/usr/bin/bash", "-c", setup], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    generated = guest / "boot/grub2/grub.cfg"
    assert generated.exists(), "setup never generated a serial-enabled GRUB configuration"
    terminal, inputs, outputs, serial, style, timeout, cmdline, bls = (
        generated.read_text().splitlines()
    )
    assert terminal == "", "legacy GRUB_TERMINAL must not override serial input/output"
    assert "serial" in inputs.split() and "serial" in outputs.split()
    expected_speed = speed if cmdline_state == "populated" else "115200"
    assert f"--speed={expected_speed}" in shlex.split(serial)
    assert "--unit=0" in shlex.split(serial)
    assert style == "menu" and int(timeout) >= 5
    assert cmdline.split() == [*default_args.split(), "systemd.unit=rescue.target"]
    assert bls == "true"
    assert entry.read_text() == f"options {platform} systemd.unit=rescue.target\n"
    calls = log.read_text().splitlines()
    install = next(line for line in calls if line.startswith("dnf "))
    assert "grub2-tools" in install.split()
    assert "grub2-editenv - unset menu_auto_hide" in calls
    assert calls.index("grub2-editenv - unset menu_auto_hide") < next(
        i for i, line in enumerate(calls) if line.startswith("grub2-mkconfig ")
    )
    assert "passwd -l root" in calls and "passwd -u root" not in calls
    assert "UUID=00000000-0000-0000-0000-000000000310" in (guest / "etc/fstab").read_text()
    assert (guest / "srv/lx310/ledger.txt").read_text() == "LX310 retained ledger\n"
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in guest.rglob("*") if p.is_file()}
    result = subprocess.run(
        ["/usr/bin/bash", "-c", setup], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert before == {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in guest.rglob("*") if p.is_file()
    }
    assert log.read_text().splitlines() == calls
