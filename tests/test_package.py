import re
import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import pytest

import labctl
from labctl.errors import ExitStatus


def test_public_documentation_uses_portable_operator_home_paths() -> None:
    root = Path(__file__).parents[1]
    documents = [*root.glob("*.md"), *root.joinpath("docs").rglob("*.md")]
    # These are deliberate guest exercise accounts, not operator identities.
    guest_accounts = {"student", "deploy"}
    violations = []
    for document in documents:
        for line_number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1):
            accounts = re.findall(r"/(?:home|Users)/([A-Za-z0-9_.-]+)", line)
            if any(account not in guest_accounts for account in accounts):
                violations.append(f"{document.relative_to(root)}:{line_number}")
    assert not violations, "Use $HOME for operator paths: " + ", ".join(violations)


def test_version_and_console_entry_point() -> None:
    assert labctl.__version__ == "0.1.0"
    scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
    assert scripts["labctl"] == "labctl.cli:main"


@pytest.mark.parametrize(
    "filename",
    ["ARCH_INSTALL.md", "FEDORA44_INSTALL.md", "CENTOS_STREAM_10_INSTALL.md"],
)
def test_install_guide_supports_a_source_checkout(filename: str) -> None:
    guide = Path(filename).read_text(encoding="utf-8")

    assert "A Git clone does not contain generated release artefacts" in guide
    assert 'cd labctl\npipx install "$PWD"\n' in guide
    assert 'export PATH="$HOME/.local/bin:$PATH"' in guide
    assert "labctl provider doctor\n" in guide
    assert "labctl image pull rocky:9\n" in guide
    assert "labctl lab create LX001\n" in guide
    assert "labctl lab ssh LX001\n" in guide
    assert "labctl lab grade LX001\n" in guide


def test_stable_exit_status_taxonomy() -> None:
    assert {status.name: status.value for status in ExitStatus} == {
        "SUCCESS": 0,
        "USAGE": 2,
        "NOT_FOUND": 3,
        "CONFLICT": 4,
        "PREREQUISITE": 5,
        "OPERATION": 6,
        "GRADING": 7,
    }


def test_packaging_is_python_only() -> None:
    root = Path(__file__).parents[1]
    metadata = tomllib.loads(root.joinpath("pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["build-system"]["build-backend"] == "hatchling.build"
    assert "rpm" not in metadata["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert not root.joinpath("rpm").exists()


def test_license_metadata_is_consistent() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    license_text = Path("LICENSE").read_text(encoding="utf-8")

    assert project["license"] == "BSD-2-Clause"
    assert license_text.startswith("BSD 2-Clause License\n")
