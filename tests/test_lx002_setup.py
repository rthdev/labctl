"""Execute LX002 setup and grader predicates in an unprivileged sandbox."""

from __future__ import annotations

import runpy
import shlex
import subprocess
from pathlib import Path

LAB = Path(__file__).parents[1] / "src/labctl/data/labs/LX002"


def test_lx002_fresh_setup_leaves_hierarchy_for_learner(tmp_path: Path) -> None:
    root = tmp_path / "lx002"
    home = tmp_path / "student"
    home.mkdir()
    setup = (LAB / "setup.sh").read_text(encoding="utf-8")
    setup = setup.replace("/srv/lx002", str(root)).replace("/home/student", str(home))
    # Only ownership changes are shimmed; execute real filesystem operations.
    script = (
        "install() {\n"
        '  printf \'%s\\n\' "$*" >> "$INSTALL_LOG"\n'
        "  shift 7\n"
        '  /usr/bin/install -d -m 0755 "$@"\n'
        "}\n"
        "chown() { :; }\n" + setup
    )
    log = tmp_path / "install.log"
    checks = runpy.run_path(str(LAB / "grade.py"))["CHECKS"]

    def check(command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            shlex.split(
                command.replace("/srv/lx002", str(root)).replace("/home/student", str(home))
            ),
            capture_output=True,
            text=True,
            check=False,
        )

    for _ in range(2):
        subprocess.run(
            ["/bin/sh", "-eu", "-c", script],
            env={"PATH": "/usr/bin:/bin", "INSTALL_LOG": str(log)},
            check=True,
            capture_output=True,
            text=True,
        )
        assert (root / "source/records.txt").read_text().splitlines() == [
            "INFO api ready",
            "WARN cache cold",
            "ERROR request failed",
            "INFO worker ready",
            "ERROR database timeout",
            "WARN retry scheduled",
        ]
        for description, command, expected in checks:
            result = check(command)
            assert not (result.returncode == 0 and result.stdout.strip() == expected), description
    # The learner must own the parent to create archive without sudo.
    assert any(str(root) in shlex.split(line)[7:] for line in log.read_text().splitlines())
    (root / "archive").mkdir()
    result = check(checks[0][1])
    assert result.returncode == 0
    assert result.stdout == ""
