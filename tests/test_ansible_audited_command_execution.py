"""Execute audited commands locally; no guest or host service mutations."""

from __future__ import annotations

import importlib.util
import shlex
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

LABS = Path(__file__).parents[1] / "src/labctl/data/labs"


def load_grader(lab: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"audit_{lab}", LABS / lab / "grade.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("directory", [True, False])
def test_an002_real_directory_command(tmp_path: Path, directory: bool) -> None:
    path = tmp_path / "shared directory"
    if directory:
        path.mkdir()
    else:
        path.touch()
    command = load_grader("AN002").CHECKS["shared path is a directory"][0]
    command = command.replace("/srv/automation", shlex.quote(str(path)))
    result = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)
    assert result.returncode == (0 if directory else 1), result.stderr


def accepts_recap(lab: str, output: str) -> bool:
    module = load_grader(lab)
    if lab == "AN102":
        return bool(module._recap_reports_no_changes(output))
    if lab == "AN401":
        return bool(module._idempotent_recap(output, {"target"}))
    return bool(module._idempotent_recap(output))


@pytest.mark.parametrize("lab", ["AN102", "AN401", "AN402"])
@pytest.mark.parametrize("changed", [False, True])
def test_real_local_ansible_controller_play_recap(tmp_path: Path, lab: str, changed: bool) -> None:
    # Debug tasks run locally without touching host services or learner files.
    import os
    import shutil

    ansible = shutil.which("ansible-playbook")
    if ansible is None:
        pytest.skip("local ansible-playbook is not installed")
    hosts = ["web1", "web2", "web3"] if lab == "AN402" else ["target"]
    inventory = tmp_path / "inventory"
    inventory.write_text("\n".join(hosts) + "\n")
    playbook = tmp_path / "site.yml"
    playbook.write_text(
        "- hosts: all\n  connection: local\n  gather_facts: false\n"
        "  tasks:\n    - ansible.builtin.debug:\n        msg: managed\n"
        "- hosts: localhost\n  connection: local\n  gather_facts: false\n"
        "  tasks:\n    - ansible.builtin.debug:\n        msg: controller\n"
        f"      changed_when: {str(changed).lower()}\n"
    )
    config = tmp_path / "ansible.cfg"
    config.write_text("[defaults]\nstdout_callback = default\n")
    result = subprocess.run(
        [ansible, "-i", str(inventory), str(playbook)],
        env=os.environ
        | {
            "ANSIBLE_CONFIG": str(config),
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_STDOUT_CALLBACK": "default",
            "ANSIBLE_FORCE_COLOR": "0",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "localhost" in result.stdout
    assert accepts_recap(lab, result.stdout) is (lab == "AN402" or not changed), result.stdout


@pytest.mark.parametrize("lab", ["AN102", "AN401", "AN402"])
@pytest.mark.parametrize("field", ["changed", "failed", "unreachable"])
def test_recap_checks_lab_specific_controller_outcomes(lab: str, field: str) -> None:
    hosts = ["web1", "web2", "web3"] if lab == "AN402" else ["target"]
    clean = "ok=1 changed=0 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0"
    recap = "PLAY RECAP ****\n" + "".join(f"{host} : {clean}\n" for host in hosts)
    recap += "localhost : " + clean.replace(f"{field}=0", f"{field}=1") + "\n"
    assert accepts_recap(lab, recap) is (lab == "AN402" and field == "changed")


@pytest.mark.parametrize("lab", ["AN102", "AN401", "AN402"])
def test_recap_requires_managed_hosts_even_with_localhost(lab: str) -> None:
    assert not accepts_recap(
        lab, "PLAY RECAP ****\nlocalhost : ok=1 changed=0 unreachable=0 failed=0\n"
    )
