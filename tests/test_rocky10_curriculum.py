"""Rocky 10 curriculum cardinality and documentation contract."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from labctl.definitions import load_definition

ROOT = Path(__file__).parents[1]
LABS = ROOT / "src/labctl/data/labs"
EXPECTED = {f"LX{level}1{index}" for level in range(5) for index in range(3)}
LEGACY = {"LX001", "LX002", "LX101", "LX102", "LX201", "LX202", "LX301", "LX302", "LX401", "LX402"}


def test_exact_rocky10_linux_curriculum() -> None:
    definitions = [load_definition(path) for path in sorted(LABS.glob("LX*/lab.yaml"))]
    actual = {d.id for d in definitions if all(vm.image == "rocky:10" for vm in d.vms)}
    assert actual == EXPECTED
    assert Counter(lab_id[2] for lab_id in actual) == dict.fromkeys("01234", 3)
    assert {d.id for d in definitions} >= LEGACY


def test_curriculum_documents_all_labs_and_official_sources() -> None:
    path = ROOT / "docs/rocky10-linux-curriculum.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    for lab_id in EXPECTED:
        assert f"| {lab_id} |" in text
    assert "https://www.redhat.com/en/services/training/ex200-" in text
    assert "https://www.redhat.com/en/services/training/ex342-" in text
    assert "not a complete exam preparation" in text
