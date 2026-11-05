"""Focused integration-review assignment/predicate agreement; no PAM calls."""

# ruff: noqa: S102
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from labctl.definitions import load_definition

LABS = Path(__file__).parents[1] / "src/labctl/data/labs"


def test_lx210_persistent_mount_contract_is_explicit():
    instructions = load_definition(LABS / "LX210/lab.yaml").instructions
    assert "exactly one" in instructions
    assert "FSTYPE `xfs`" in instructions
    assert all(option in instructions for option in ("ro", "noauto", "nofail"))


@pytest.mark.parametrize(
    "fstype,entries,accepted",
    [("xfs", 1, True), ("ext4", 1, False), ("xfs", 2, False), ("xfs", 0, False)],
)
def test_lx210_actual_findmnt_fstab_parser(tmp_path, fstype, entries, accepted):
    path = LABS / "LX210/grade.py"
    grader = {"__name__": "review_test"}
    exec(compile(path.read_text(), str(path), "exec"), grader)
    fstab = tmp_path / "fstab"
    fstab.write_text(f"/dev/vg210/records /srv/lx210 {fstype} defaults 0 0\n" * entries)
    fixture = """
losetup() { printf '/dev/loop999\\n'; }
lvs() { printf '402653184\\n'; }
readlink() { printf '%s\\n' "${@: -1}"; }
findmnt() { /usr/bin/findmnt --tab-file "$FSTAB" "$@"; }
"""
    result = subprocess.run(
        [
            "/usr/bin/bash",
            "-euo",
            "pipefail",
            "-c",
            fixture + shlex.split(grader["CHECKS"][2][1])[-1],
        ],
        env={**os.environ, "FSTAB": str(fstab)},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert (result.returncode == 0 and result.stdout.strip() == "ok") is accepted


def test_lx212_explains_observed_replacement_contract():
    instructions = load_definition(LABS / "LX212/lab.yaml").instructions
    assert "open descriptor" in instructions
    assert "old bytes" in instructions
    assert "crash" in instructions
    assert "OnBootSec=10s" in instructions
    assert "OnUnitActiveSec=10s" in instructions
    assert "no additional" in instructions
    assert "no randomized delay" in instructions
    assert "equivalent duration" in instructions


def run_lx212_timer(tmp_path, properties, *, producer_rc=0, calendar="", calendar_rc=0):
    """Execute the shipped check; model systemd and time, not its predicate."""
    path = LABS / "LX212/grade.py"
    grader = {"__name__": "review_test"}
    exec(compile(path.read_text(), str(path), "exec"), grader)
    command = shlex.split(grader["CHECKS"][2][1])[-1]
    command = command.replace("/var/lib/lx212", str(tmp_path))
    command = command.replace("root:root 640", f"{os.getuid()}:{os.getgid()} 640")
    command = command.replace("%U:%G %a", "%u:%g %a")
    (tmp_path / "events").write_text("ERROR one\nERROR two\nWARN three\nINFO four\n")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/python3\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "if args[0] == 'is-enabled': print('enabled')\n"
        "elif args[0] == 'is-active': print('active')\n"
        "elif 'Triggers' in args: print('lx212-report.service')\n"
        "elif 'Result' in args: print('success')\n"
        "elif 'TimersCalendar' in args:\n"
        "    print(os.environ['CALENDAR'])\n"
        "    sys.exit(int(os.environ['CALENDAR_RC']))\n"
        "else:\n"
        "    for line in os.environ['PROPERTIES'].splitlines():\n"
        "        if line.split('=', 1)[0] in args: print(line)\n"
        "    sys.exit(int(os.environ['PRODUCER_RC']))\n"
    )
    systemctl.chmod(0o700)
    fixture = r"""
    sleep() {
        hash=$(sha256sum "$FIXTURE/events" | cut -d ' ' -f1)
        printf 'ERROR=2\nWARN=1\nINFO=1\nSHA256=%s\n' "$hash" > "$FIXTURE/new"
        chmod 640 "$FIXTURE/new"
        mv "$FIXTURE/new" "$FIXTURE/report"
    }
    """
    return subprocess.run(
        ["/usr/bin/bash", "-euo", "pipefail", "-c", fixture + command],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FIXTURE": str(tmp_path),
            "PROPERTIES": properties,
            "PRODUCER_RC": str(producer_rc),
            "CALENDAR": calendar,
            "CALENDAR_RC": str(calendar_rc),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


TIMER_PROPERTIES = """TimersMonotonic={ OnUnitActiveUSec=10s ; next_elapse=2w 23min 38.560747s }
TimersMonotonic={ OnBootUSec=10s ; next_elapse=10s }
AccuracyUSec=1s
RandomizedDelayUSec=0"""


def test_lx212_rejects_wrong_effective_interval(tmp_path):
    result = run_lx212_timer(tmp_path, TIMER_PROPERTIES.replace("=10s", "=20s"))
    assert result.returncode != 0
    assert "ok" not in result.stdout.splitlines()


@pytest.mark.parametrize(
    "properties",
    [
        TIMER_PROPERTIES,
        "\n".join(reversed(TIMER_PROPERTIES.splitlines())),
        TIMER_PROPERTIES.replace("=10s", "=10000ms").replace("=1s", "=1000000us"),
        TIMER_PROPERTIES.replace("=10s", "=9s 1000ms").replace("=1s", "=0.5s 500ms"),
        TIMER_PROPERTIES.replace("=10s", "=10.000000s").replace("=1s", "=1000000µs"),
    ],
)
def test_lx212_accepts_equivalent_effective_timing(tmp_path, properties):
    result = run_lx212_timer(tmp_path, properties)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"


def test_lx212_rejects_inaccurate_timer(tmp_path):
    result = run_lx212_timer(
        tmp_path, TIMER_PROPERTIES.replace("AccuracyUSec=1s", "AccuracyUSec=2s")
    )
    assert result.returncode != 0


@pytest.mark.parametrize(
    "properties,producer_rc,calendar,calendar_rc",
    [
        (TIMER_PROPERTIES, 1, "", 0),
        (TIMER_PROPERTIES, 0, "", 1),
        (TIMER_PROPERTIES, 0, "{ OnCalendar=*-*-* *:*:00 ; next_elapse=never }", 0),
        (TIMER_PROPERTIES.replace("RandomizedDelayUSec=0", "RandomizedDelayUSec=1s"), 0, "", 0),
        ("", 0, "", 0),
        ("\n".join(TIMER_PROPERTIES.splitlines()[1:]), 0, "", 0),
        (TIMER_PROPERTIES.replace("OnUnitActiveUSec", "OnUnitInactiveUSec"), 0, "", 0),
        (TIMER_PROPERTIES + "\nTimersMonotonic={ OnActiveUSec=10s ; next_elapse=10s }", 0, "", 0),
        (TIMER_PROPERTIES + "\n" + TIMER_PROPERTIES.splitlines()[0], 0, "", 0),
        (TIMER_PROPERTIES.replace("AccuracyUSec=1s\n", ""), 0, "", 0),
        (TIMER_PROPERTIES.replace("RandomizedDelayUSec=0", ""), 0, "", 0),
        (TIMER_PROPERTIES + "\nAccuracyUSec=1s", 0, "", 0),
        (TIMER_PROPERTIES.replace("AccuracyUSec=1s", "AccuracyUSec=infinity"), 0, "", 0),
        (TIMER_PROPERTIES.replace("=10s", "=10s garbage"), 0, "", 0),
        (TIMER_PROPERTIES.replace("=10s", "=10.000001s"), 0, "", 0),
        (TIMER_PROPERTIES.replace("AccuracyUSec=1s", "AccuracyUSec=999ms"), 0, "", 0),
        (TIMER_PROPERTIES.replace("RandomizedDelayUSec=0", "RandomizedDelayUSec="), 0, "", 0),
    ],
    ids=[
        "producer-error",
        "calendar-producer-error",
        "extra-calendar",
        "random-delay",
        "empty",
        "missing-boot",
        "inactive-not-active",
        "extra-monotonic",
        "duplicate-timer",
        "missing-accuracy",
        "missing-delay",
        "duplicate-accuracy",
        "infinite-accuracy",
        "malformed-duration",
        "fractional-mismatch",
        "different-accuracy",
        "empty-delay",
    ],
)
def test_lx212_rejects_invalid_effective_timing(
    tmp_path, properties, producer_rc, calendar, calendar_rc
):
    result = run_lx212_timer(
        tmp_path, properties, producer_rc=producer_rc, calendar=calendar, calendar_rc=calendar_rc
    )
    assert result.returncode != 0
    assert "ok" not in result.stdout.splitlines()
    # Failed timing evidence must stop before the report removal/regeneration probe.
    assert not (tmp_path / "report").exists()


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("control", ["required", "requisite"])
def test_lx410_equivalent_module_order_matches_assignment(tmp_path, reverse, control):
    instructions = load_definition(LABS / "LX410/lab.yaml").instructions
    assert "either order" in instructions
    path = LABS / "LX410/grade.py"
    grader = {"__name__": "review_test"}
    exec(compile(path.read_text(), str(path), "exec"), grader)
    policy = {}
    exec(grader["PAM_EVIDENCE"], policy)
    access = tmp_path / "access"
    access.write_text("+ : (lx410-operators) : ALL\n- : ALL : ALL\n")
    rows = [
        f"account {control} pam_unix.so",
        f"account {control} pam_access.so accessfile={access}",
    ]
    stack = tmp_path / "stack"
    stack.write_text("\n".join(reversed(rows) if reverse else rows) + "\n")
    policy["verify_policy"](stack, access)
