from __future__ import annotations

import os
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

LABS = Path(__file__).parents[1] / "src/labctl/data/labs"


def _row(host: str, changed: int = 0) -> str:
    return f"{host} : ok=1 changed={changed} unreachable=0 failed=0 skipped=0 rescued=0 ignored=0\n"


def _managed(lab_id: str) -> tuple[str, ...]:
    return ("target",) if lab_id == "AN401" else ("web1", "web2", "web3")


def _accepts(lab_id: str, output: str) -> bool:
    grade = runpy.run_path(str(LABS / lab_id / "grade.py"))
    if lab_id == "AN401":
        return bool(grade["_idempotent_recap"](output, set(_managed(lab_id))))
    return bool(grade["_idempotent_recap"](output))


@pytest.mark.parametrize("lab_id", ["AN401", "AN402"])
@pytest.mark.parametrize("alias", ["localhost", "127.0.0.1", "::1", "report-controller"])
@pytest.mark.parametrize("changed", [0, 1])
def test_controller_alias_outcomes(lab_id: str, alias: str, changed: int) -> None:
    output = "PLAY RECAP ****\n" + "".join(_row(host) for host in _managed(lab_id))
    output += _row(alias, changed)
    assert _accepts(lab_id, output) == (lab_id == "AN402" or changed == 0)


@pytest.mark.parametrize("lab_id", ["AN401", "AN402"])
@pytest.mark.parametrize(
    "defect",
    [
        "no-header",
        "duplicate-row",
        "duplicate-counter",
        "malformed-row",
        "negative",
        "suffix",
        "missing-counter",
        "bad-ok",
        "missing-host",
        "empty-final",
        "partial-final",
    ],
)
def test_final_recap_is_strict(lab_id: str, defect: str) -> None:
    hosts = _managed(lab_id)
    rows = "".join(_row(host) for host in hosts)
    output = "PLAY RECAP ****\n" + rows
    if defect == "no-header":
        output = rows
    elif defect == "duplicate-row":
        output += _row(hosts[0])
    elif defect == "duplicate-counter":
        output = output.replace("failed=0", "failed=0 failed=0", 1)
    elif defect == "malformed-row":
        output += "controller : malformed\n"
    elif defect == "negative":
        output = output.replace("failed=0", "failed=-1", 1)
    elif defect == "suffix":
        output = output.replace("failed=0", "failed=0oops", 1)
    elif defect == "missing-counter":
        output = output.replace("changed=0 ", "", 1)
    elif defect == "bad-ok":
        output = output.replace("ok=1", "ok=oops", 1)
    elif defect == "missing-host":
        output = "PLAY RECAP ****\n" + "".join(_row(host) for host in hosts[1:])
    elif defect == "empty-final":
        output += "PLAY RECAP ****\n"
    else:
        output += "PLAY RECAP ****\n" + _row("controller")
    assert not _accepts(lab_id, output)


@pytest.mark.parametrize("lab_id", ["AN401", "AN402"])
@pytest.mark.parametrize("alias", ["127.0.0.1", "::1", "report-controller"])
@pytest.mark.parametrize("counter", ["failed", "unreachable"])
def test_controller_failures_are_not_ignored(lab_id: str, alias: str, counter: str) -> None:
    output = "PLAY RECAP ****\n" + "".join(_row(host) for host in _managed(lab_id))
    output += _row(alias).replace(f"{counter}=0", f"{counter}=1")
    assert not _accepts(lab_id, output)


@pytest.mark.parametrize("lab_id", ["AN401", "AN402"])
def test_only_final_recap_counts(lab_id: str) -> None:
    earlier = "PLAY RECAP ****\n" + _row("old-controller", changed=3)
    final = "PLAY RECAP ****\n" + "".join(_row(host) for host in _managed(lab_id))
    assert _accepts(lab_id, earlier + final)


@pytest.mark.parametrize("lab_id", ["AN401", "AN402"])
@pytest.mark.parametrize("alias", ["127.0.0.1", "::1", "report-controller"])
@pytest.mark.parametrize("changed", [False, True])
def test_real_local_ansible_controller_alias(
    tmp_path: Path, lab_id: str, alias: str, changed: bool
) -> None:
    executable = shutil.which("ansible-playbook")
    if executable is None:
        pytest.skip("ansible-playbook is not installed")
    inventory = tmp_path / "inventory.ini"
    inventory.write_text(
        "[managed]\n"
        + "".join(f"{host} ansible_connection=local\n" for host in _managed(lab_id))
        + f"[reporting]\n{alias} ansible_connection=local\n"
    )
    playbook = tmp_path / "site.yml"
    playbook.write_text(
        "- hosts: managed\n  gather_facts: false\n  tasks:\n"
        "    - ansible.builtin.debug:\n        msg: healthy\n"
        "- hosts: reporting\n  gather_facts: false\n  tasks:\n"
        "    - ansible.builtin.debug:\n        msg: report\n"
        f"      changed_when: {str(changed).lower()}\n"
    )
    config = tmp_path / "ansible.cfg"
    config.write_text("[defaults]\nstdout_callback=default\n")
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("ANSIBLE_")
    }
    environment.update(ANSIBLE_CONFIG=str(config), ANSIBLE_NOCOLOR="1", ANSIBLE_FORCE_COLOR="0")
    result = subprocess.run(
        [executable, "-i", str(inventory), str(playbook)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert alias in result.stdout.split("PLAY RECAP")[-1]
    assert _accepts(lab_id, result.stdout) == (lab_id == "AN402" or not changed)
