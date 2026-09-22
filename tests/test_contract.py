from __future__ import annotations

from pathlib import Path

import pytest

from labctl.cli import ParseError, parse_args
from labctl.images import ImageCatalog, ImageStore
from labctl.subprocesses import CommandResult


def test_authoritative_lab_filters_and_flags() -> None:
    assert parse_args(["lab", "ls", "--available"]).available
    assert parse_args(["lab", "ls", "--active"]).active
    with pytest.raises(ParseError, match="not allowed with"):
        parse_args(["lab", "ls", "--available", "--active"])
    create = parse_args(["lab", "create", "LX001", "--trust-external", "--allow-untrusted-image"])
    assert create.id == "LX001"
    assert create.trust_external and create.allow_untrusted_image


def test_authoritative_vm_image_and_reconcile_syntax() -> None:
    vm = parse_args(["vm", "inspect", "LX001", "node"])
    assert (vm.lab_id, vm.vm_name) == ("LX001", "node")
    assert parse_args(["vm", "ls", "--lab", "LX001"]).lab_id == "LX001"
    assert parse_args(["image", "import", "local:test", "x.qcow2", "--checksum", "a" * 64])
    assert parse_args(["lab", "reconcile", "LX001", "--repair"]).repair
    assert not parse_args(["lab", "reset", "LX001"]).force
    assert not parse_args(["lab", "rm", "LX001"]).force


class RecordingRunner:
    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
        self.argv.append(argv)
        output = '{"format":"qcow2"}' if argv[1] == "info" else "{}"
        return CommandResult(tuple(argv), 0, output, "")


def test_checksum_import_still_runs_safe_qcow_validation(tmp_path: Path) -> None:
    source = tmp_path / "disk.qcow2"
    source.write_bytes(b"fixture")
    import hashlib

    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    runner = RecordingRunner()
    ImageStore(tmp_path / "store", runner=runner).import_qcow2(
        "local:test", source, trusted_sha256=checksum
    )
    assert runner.argv == [
        ["qemu-img", "info", "--output=json", str(source)],
        ["qemu-img", "check", "--output=json", str(source)],
    ]


def test_bundled_metadata_has_complete_honest_provenance() -> None:
    for image in ImageCatalog.bundled().values():
        assert image.logical_name == image.id
        assert image.architecture == "x86_64"
        assert image.default_ssh_user
        assert image.availability in {"available", "unavailable"}
        if not image.available:
            assert image.unavailable_reason
