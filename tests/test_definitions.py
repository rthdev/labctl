from __future__ import annotations

import os
from pathlib import Path

import pytest

from labctl.definitions import DefinitionError, directory_digest, discover, load_definition


def make_definition(root: Path, lab_id: str = "LX001", depends: str = "") -> Path:
    root.mkdir(parents=True)
    (root / "setup.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "grade.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(root / "setup.sh", 0o700)
    os.chmod(root / "grade.sh", 0o700)
    dependency = f"    depends_on: [{depends}]\n" if depends else "    depends_on: []\n"
    (root / "lab.yaml").write_text(
        f"""schema_version: 1
id: {lab_id}
title: Test
goal: Learn
instructions: Do the task.
provider: kvm
grader: grade.sh
vms:
  - name: node
    cpus: 1
    ram: 2GiB
    disk: 20GiB
    image: rocky:9
    hostname: node.lab
    interfaces:
      - type: isolated_nat
    setup: setup.sh
{dependency}""",
        encoding="utf-8",
    )
    return root / "lab.yaml"


def test_load_strict_definition_and_order(tmp_path: Path) -> None:
    source = make_definition(tmp_path / "lab")
    definition = load_definition(source)
    assert definition.id == "LX001"
    assert definition.vms[0].ram_bytes == 2 * 1024**3
    assert [vm.name for vm in definition.dependency_order()] == ["node"]


def test_optional_grading_reset_vms_is_validated_and_preserves_v1_defaults(tmp_path: Path) -> None:
    source = make_definition(tmp_path / "lab")
    assert load_definition(source).grading.reset_vms == ()

    text = source.read_text(encoding="utf-8").replace(
        "grader: grade.sh\n", "grader: grade.sh\ngrading:\n  reset_vms: [node]\n"
    )
    source.write_text(text, encoding="utf-8")
    assert load_definition(source).grading.reset_vms == ("node",)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("[]", "non-empty"),
        ("[node, node]", "duplicate"),
        ("[missing]", "unknown VM"),
        ("node", "list"),
    ],
)
def test_grading_reset_vms_rejects_malformed_selections(
    tmp_path: Path, value: str, message: str
) -> None:
    source = make_definition(tmp_path / "lab")
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "grader: grade.sh\n", f"grader: grade.sh\ngrading:\n  reset_vms: {value}\n"
        ),
        encoding="utf-8",
    )
    with pytest.raises(DefinitionError, match=message):
        load_definition(source)


def test_grading_rejects_unknown_fields(tmp_path: Path) -> None:
    source = make_definition(tmp_path / "lab")
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "grader: grade.sh\n", "grader: grade.sh\ngrading:\n  surprise: true\n"
        ),
        encoding="utf-8",
    )
    with pytest.raises(DefinitionError, match=r"grading\.surprise: unknown field"):
        load_definition(source)


def test_grading_rejects_null_instead_of_treating_it_as_omitted(tmp_path: Path) -> None:
    source = make_definition(tmp_path / "lab")
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "grader: grade.sh\n", "grader: grade.sh\ngrading: null\n"
        ),
        encoding="utf-8",
    )
    with pytest.raises(DefinitionError, match="grading: must be a mapping"):
        load_definition(source)


@pytest.mark.parametrize(
    ("edit", "path"),
    [
        (lambda text: text + "surprise: true\n", "surprise"),
        (lambda text: text.replace("id: LX001", "id: bad"), "id"),
        (lambda text: text.replace("cpus: 1", "cpus: 0"), "vms.0.cpus"),
        (lambda text: text.replace("type: isolated_nat", "type: bridge"), "interfaces.0.type"),
    ],
)
def test_field_diagnostics(tmp_path: Path, edit: object, path: str) -> None:
    source = make_definition(tmp_path / "lab")
    source.write_text(edit(source.read_text(encoding="utf-8")), encoding="utf-8")  # type: ignore[operator]
    with pytest.raises(DefinitionError, match=path):
        load_definition(source)


def test_script_symlink_and_cycle_rejected(tmp_path: Path) -> None:
    source = make_definition(tmp_path / "lab")
    (tmp_path / "outside").write_text("#!/bin/sh\n", encoding="utf-8")
    (source.parent / "setup.sh").unlink()
    (source.parent / "setup.sh").symlink_to(tmp_path / "outside")
    with pytest.raises(DefinitionError, match="symlink"):
        load_definition(source)

    source = make_definition(tmp_path / "cycle")
    text = source.read_text(encoding="utf-8").replace("depends_on: []", "depends_on: [node]")
    source.write_text(text, encoding="utf-8")
    with pytest.raises(DefinitionError, match="self-dependency"):
        load_definition(source)


def test_digest_and_duplicate_discovery(tmp_path: Path) -> None:
    one = make_definition(tmp_path / "one")
    two = make_definition(tmp_path / "two")
    assert directory_digest(one.parent) == directory_digest(one.parent)
    with pytest.raises(DefinitionError, match="duplicate external"):
        discover([], [one.parent, two.parent])
