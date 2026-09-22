from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from labctl.subprocesses import Runner


def test_runner_debug_log_excludes_captured_command_output(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    script = tmp_path / "command.py"
    script.write_text("print('captured-sensitive-output')\n", encoding="utf-8")
    caplog.set_level(logging.DEBUG, logger="labctl.subprocesses")

    result = Runner().run([sys.executable, str(script)])

    assert result.stdout.strip() == "captured-sensitive-output"
    assert "executing command:" in caplog.text
    assert "captured-sensitive-output" not in caplog.text
