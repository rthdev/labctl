"""Canonical assignment publication at the real cloud-init seed boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from test_orchestrator import (
    FakeRunner,
    ResetFileRunner,
    _definition,
    _host_keys,
    _image,
    _keys,
    _single_reset_fixture,
)

from labctl.cloudinit import generate_cloud_init
from labctl.definitions import load_definition
from labctl.images import ImageStore
from labctl.orchestrator import KVMOrchestrator


@pytest.mark.parametrize("peer_user", ["learner", "root"])
def test_create_publishes_unchanged_instructions_on_every_vm(
    tmp_path: Path, peer_user: str
) -> None:
    definition = _definition(tmp_path)
    raw = yaml.safe_load(definition.source.read_text())
    body = (
        "\n## Assignment\n\n```sh\n$HOME 'quoted' \"double\" `id` $(id) \\\nEOF\n```\n: # café\n\n"
    )
    raw["title"] = "Literal: # title"
    raw["instructions"] = body
    raw["vms"][0].pop("ssh_user")
    peer = deepcopy(raw["vms"][0])
    peer.update(name="peer", hostname="peer.lab", ssh_user=peer_user)
    raw["vms"].append(peer)
    definition.source.write_text(yaml.safe_dump(raw))
    definition = load_definition(definition.source)
    runner = FakeRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    orchestrator = KVMOrchestrator(
        tmp_path / "data", tmp_path / "state", runner=runner, keygen=_keys, host_keygen=_host_keys
    )
    state = orchestrator.create(
        definition, "qemu:///system", images, readiness_probe=lambda *_: True
    )
    calls = [call for call in runner.calls if call[0] == "cloud-localds"]
    assert len(calls) == len(definition.vms) == 2
    for call, vm in zip(calls, definition.dependency_order(), strict=True):
        document = yaml.safe_load(Path(call[2]).read_text())
        user = vm.ssh_user or "student"
        home = "/root" if user == "root" else f"/home/{user}"
        guides = [entry for entry in document["write_files"] if entry["path"].endswith("/LAB.md")]
        assert guides == [
            {
                "path": f"{home}/LAB.md",
                "owner": f"{user}:{user}",
                "permissions": "0644",
                # write_files otherwise runs before users-groups on common images.
                "defer": True,
                "content": f"# LX001: Literal: # title\n\n{body}",
            }
        ]
        assert document["users"][0]["name"] == user
        assert body not in repr(document["runcmd"])
        assert all(
            body not in entry["content"]
            for entry in document["write_files"]
            if entry["path"] != f"{home}/LAB.md"
        )
        assert state["vms"][vm.name]["seed"] == call[1]


@pytest.mark.parametrize("body", ["No trailing newline.", "\nKeep whitespace.  \n\n"])
def test_standalone_renderer_shares_canonical_assignment(tmp_path: Path, body: str) -> None:
    definition = replace(_definition(tmp_path), instructions=body)
    document = yaml.safe_load(
        generate_cloud_init("ssh-ed25519 AAAA lab", ["true"], definition=definition)
    )
    real_document = yaml.safe_load(
        KVMOrchestrator._cloud_init(
            definition.vms[0], "ssh-ed25519 AAAA lab", "private", "public", definition=definition
        )
    )
    guide = next(entry for entry in document["write_files"] if entry["path"].endswith("/LAB.md"))
    assert guide in real_document["write_files"]
    assert guide["content"] == f"# LX001: Test\n\n{body}"
    assert guide["defer"] is True


def test_render_uses_loaded_definition_not_mutable_source(tmp_path: Path) -> None:
    definition = _definition(tmp_path)
    definition.source.write_text("not a definition anymore")
    document = yaml.safe_load(
        KVMOrchestrator._cloud_init(
            definition.vms[0], "ssh-ed25519 AAAA lab", "private", "public", definition=definition
        )
    )
    guide = next(entry for entry in document["write_files"] if entry["path"].endswith("/LAB.md"))
    assert guide["content"] == "# LX001: Test\n\nSolve it."


@pytest.mark.parametrize("selected", [None, ("node",)])
@pytest.mark.parametrize("legacy", [False, True])
def test_reset_preserves_saved_assignment_seed_and_snapshot(
    tmp_path: Path, selected: tuple[str, ...] | None, legacy: bool
) -> None:
    domain, overlay, images, _ = _single_reset_fixture(tmp_path)
    definition = _definition(tmp_path)
    snapshot = tmp_path / "data/instances/LX001/definition"
    # Preserve old setup-authored guides as well as new central assignments.
    import shutil

    shutil.copytree(definition.source.parent, snapshot)
    snapshot_setup = snapshot / "setup.sh"
    snapshot_setup.write_text("#!/bin/sh\n# old setup owns its guide\n")
    before = {path.name: path.read_bytes() for path in snapshot.iterdir()}
    original = generate_cloud_init(
        "ssh-ed25519 AAAA lab", ["true"], definition=None if legacy else definition
    ).encode()
    seed = overlay.with_name("seed.iso")
    user_data = overlay.with_name("user-data")
    seed.write_bytes(original)
    user_data.write_bytes(original)
    definition.source.write_text("installed catalogue has changed")
    runner = ResetFileRunner({domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    orchestrator.reset("LX001", images, vm_names=selected, readiness_probe=lambda *_: True)
    assert seed.read_bytes() == user_data.read_bytes() == original
    assert {path.name: path.read_bytes() for path in snapshot.iterdir()} == before
    assert not any(call[0] == "cloud-localds" for call in runner.calls)
    assert any(str(seed) in arg for call in runner.calls for arg in call)
