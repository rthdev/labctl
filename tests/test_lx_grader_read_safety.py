"""Execute generated LX shell commands with narrowly modeled guest privileges."""

from __future__ import annotations

import os
import runpy
import shlex
import subprocess
from pathlib import Path

import pytest

LABS = Path(__file__).parents[1] / "src/labctl/data/labs"


def _check(lab: str, description: str) -> tuple[str, str]:
    checks = runpy.run_path(str(LABS / lab / "grade.py"))["CHECKS"]
    return next((check[1], check[2]) for check in checks if check[0] == description)


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return path


@pytest.mark.parametrize(
    ("listing", "status", "passes"),
    [
        ("", 1, False),
        ("unrelated_t\n", 127, False),
        ("httpd_t\n", 1, False),
        ("", 0, True),
        ("Customized Permissive Types\n\nother_t\n", 0, True),
        ("httpd_t\n", 0, False),
        ("other_t\nhttpd_t\nlast_t\n", 0, False),
        ("httpd_tmp_t\nnot_httpd_t\nhttpd_t_extra\n", 0, True),
    ],
)
def test_lx301_permissive_query_fails_closed(
    tmp_path: Path, listing: str, status: int, passes: bool
) -> None:
    _script(
        tmp_path / "semanage",
        '[ "$*" = "permissive -l" ] || exit 99\nprintf "%s" "$LISTING"\nexit "$STATUS"\n',
    )
    sudo = _script(tmp_path / "sudo", '[ "$1" != -n ] || shift\nexec "$@"\n')
    command, expected = _check("LX301", "httpd domain is not permissive")
    result = subprocess.run(
        ["/bin/sh", "-c", command.replace("/usr/bin/sudo", shlex.quote(str(sudo)))],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "LISTING": listing,
            "STATUS": str(status),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stderr == ""
    assert (result.returncode == 0 and result.stdout.strip() == expected) is passes


@pytest.mark.parametrize(
    ("lab", "description", "guest_path", "content", "reader"),
    [
        (
            "LX202",
            "maintenance wrote durable evidence",
            "/var/log/lx202-maintenance.log",
            "maintenance complete\n",
            "cat",
        ),
        (
            "LX402",
            "incident report contains exactly the required evidence",
            "/var/log/lx402-incident-report",
            "root cause: disk pressure and runaway burner\n"
            "recovery: capacity restored; api enabled and healthy\n",
            "diff",
        ),
    ],
)
@pytest.mark.parametrize("scenario", ["valid", "wrong", "missing", "sudo-denied"])
def test_protected_evidence_uses_narrow_noninteractive_reads(
    tmp_path: Path,
    lab: str,
    description: str,
    guest_path: str,
    content: str,
    reader: str,
    scenario: str,
) -> None:
    evidence = tmp_path / "evidence"
    if scenario != "missing":
        evidence.write_text(content if scenario != "wrong" else "wrong\n", encoding="utf-8")
        evidence.chmod(0o600)
    # Privilege is modeled, not granted: deny direct reads even if the test
    # runner owns the fixture. Run real cat/diff only after the sudo shim.
    read_shim = _script(
        tmp_path / reader,
        '[ "$PRIVILEGED" = yes ] || { echo "Permission denied" >&2; exit 1; }\n'
        f'exec /usr/bin/{reader} "$@"\n',
    )
    sudo = _script(
        tmp_path / "sudo",
        '[ "$1" = -n ] || exit 98\nshift\n'
        '[ "$DENY_SUDO" != yes ] || exit 1\n'
        # Only the specific reader is allowed, never a privileged shell.
        f'[ "$1" = {shlex.quote(str(read_shim))} ] || exit 97\n'
        'export PRIVILEGED=yes\nexec "$@"\n',
    )
    command, expected = _check(lab, description)
    command = command.replace(guest_path, str(evidence))
    command = command.replace("/usr/bin/sudo", shlex.quote(str(sudo)))
    command = command.replace(f"/usr/bin/{reader}", str(read_shim))
    # The historical diff used PATH lookup; map that form as well.
    command = command.replace("| diff ", f"| {read_shim} ")
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        env={
            **os.environ,
            "PRIVILEGED": "no",
            "DENY_SUDO": "yes" if scenario == "sudo-denied" else "no",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0 and result.stdout.strip() == expected) is (scenario == "valid")
    if scenario != "missing":
        assert evidence.stat().st_mode & 0o777 == 0o600
