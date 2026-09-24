"""Execute controller setup with guest paths and privileged commands sandboxed."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from labctl.definitions import load_definition

LABS = Path(__file__).parents[1] / "src/labctl/data/labs"
DEFINITIONS = sorted(LABS.glob("AN*/lab.yaml"))


@pytest.mark.parametrize("definition_path", DEFINITIONS, ids=lambda path: path.parent.name)
def test_controller_setup_installs_and_preserves_instructions(
    tmp_path: Path, definition_path: Path
) -> None:
    definition = load_definition(definition_path)
    controller = next(vm for vm in definition.vms if vm.name == "controller")
    home = tmp_path / "student"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shims = {
        "dnf": "#!/bin/sh\nexit 0\n",
        "chown": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CHOWN_LOG"\n',
        "runuser": '#!/bin/sh\nshift 3\nexec "$@"\n',
        "install": """#!/usr/bin/python3
import os
import sys
args = iter(sys.argv[1:])
clean = []
for arg in args:
    if arg in ('-o', '-g'):
        next(args)
    else:
        clean.append(arg)
os.execv('/usr/bin/install', ['install', *clean])
""",
    }
    for name, content in shims.items():
        shim = bindir / name
        shim.write_text(content)
        shim.chmod(0o700)
    script = tmp_path / "setup.sh"
    script.write_text(controller.setup.read_text().replace("/home/student", str(home)))
    env = {
        **os.environ,
        "PATH": f"{bindir}:/usr/bin:/bin",
        "CHOWN_LOG": str(tmp_path / "chown.log"),
    }
    guide = home / "LAB.md"
    for iteration in range(2):
        result = subprocess.run(
            ["/bin/sh", str(script)], env=env, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        assert guide.is_file(), f"{definition.id} controller is missing LAB.md"
        if iteration == 0:
            text = guide.read_text()
            assert f"# {definition.id}" in text
            assert definition.instructions.strip().replace("/home/student", str(home)) in text
            assert f"labctl grade {definition.id}" in text
            assert "on the host" in text
            assert "ansible all -i inventory.ini -m ping" in text
            assert "ansible-playbook -i inventory.ini site.yml" in text
            for target in definition.grading.reset_vms:
                assert f"ssh {target}" in text
            assert guide.stat().st_mode & 0o777 == 0o644
            assert f"student:student {guide}" in (tmp_path / "chown.log").read_text()
            guide.write_text("Learner notes: preserve me.\n")
        else:
            assert guide.read_text() == "Learner notes: preserve me.\n"
