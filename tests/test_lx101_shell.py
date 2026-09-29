"""Exercise the journald command through both real shell parsing layers."""

from __future__ import annotations

import os
import runpy
import shlex
import subprocess
from pathlib import Path

import pytest

LAB = Path(__file__).parents[1] / "src/labctl/data/labs/LX101"


@pytest.mark.parametrize(
    ("config", "status", "passes"),
    [
        ("[Journal]\nStorage=persistent\n", 0, True),
        ("  Storage = persistent  \n", 0, True),
        ("Storage=volatile\nStorage=persistent\n", 0, True),
        ("Storage=persistent\nStorage=volatile\n", 0, False),
        ("# Storage=persistent\n", 0, False),
        ("", 0, False),
        ("Storage=persistent\n", 1, False),
    ],
)
def test_journald_persistence_shell(tmp_path: Path, config: str, status: int, passes: bool) -> None:
    analyzer = tmp_path / "systemd-analyze"
    analyzer.write_text(
        '#!/bin/sh\n[ "$*" = "cat-config systemd/journald.conf" ] || exit 99\n'
        'printf "%s" "$CONFIG"\nexit "$STATUS"\n',
        encoding="utf-8",
    )
    analyzer.chmod(0o700)
    # Replace only sudo with a transparent unprivileged shim. The command's
    # nested Bash and awk quoting must be executed, not answered by fake SSH.
    sudo = tmp_path / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    sudo.chmod(0o700)
    checks = runpy.run_path(str(LAB / "grade.py"))["CHECKS"]
    command, expected = next(
        (command, expected)
        for description, command, expected in checks
        if description == "journald persistence is effective"
    )
    command = command.replace("/usr/bin/sudo", shlex.quote(str(sudo)), 1)
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "CONFIG": config,
            "STATUS": str(status),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stderr == ""
    assert (result.returncode == 0 and result.stdout.strip() == expected) is passes
