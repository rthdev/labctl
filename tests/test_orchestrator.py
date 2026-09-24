from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import uuid
from copy import deepcopy
from pathlib import Path

import pytest
from defusedxml import ElementTree

import labctl.orchestrator as orchestrator_module
from labctl.definitions import load_definition
from labctl.images import ImageStore
from labctl.locks import file_lock
from labctl.orchestrator import KVMOrchestrator, OrchestrationError
from labctl.state import load_state
from labctl.subprocesses import CommandResult


class FakeRunner:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_on = fail_on
        self.domain_metadata: dict[str, str] = {}

    def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
        call = tuple(argv)
        self.calls.append(call)
        if self.fail_on and self.fail_on in argv:
            raise RuntimeError(f"failed {self.fail_on}")
        stdout = ""
        returncode = 0
        if "domifaddr" in argv:
            stdout = " vnet0  52:54:00:00:00:01  ipv4  192.0.2.10/24\n"
        if "metadata" in argv and "--set" in argv:
            self.domain_metadata[argv[4]] = argv[argv.index("--set") + 1]
        if "metadata" in argv and "--set" not in argv:
            stdout = self.domain_metadata.get(argv[4], "")
        if "net-dumpxml" in argv:
            network = argv[4]
            uid = network.removeprefix("labctl-").removesuffix("-network")
            stdout = (
                "<network><metadata>" + _ownership_xml(uid, "network") + "</metadata></network>"
            )
        if "domstate" in argv:
            stdout = "shut off\n"
        if "net-info" in argv or "dominfo" in argv:
            returncode = 1
            kind = "network" if "net-info" in argv else "domain"
            return CommandResult(call, returncode, stdout, f"failed to get {kind} '{argv[-1]}'")
        return CommandResult(call, returncode, stdout, "")


def _definition(tmp_path: Path):  # type: ignore[no-untyped-def]
    root = tmp_path / "definition"
    root.mkdir()
    for script in ("setup.sh", "grade.sh"):
        path = root / script
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o700)
    (root / "lab.yaml").write_text(
        """schema_version: 1
id: LX001
title: Test
goal: Test provisioning
instructions: Solve it.
provider: kvm
grader: grade.sh
vms:
  - name: node
    cpus: 1
    ram: 1GiB
    disk: 8GiB
    image: local:test
    hostname: node.lab
    interfaces:
      - type: isolated_nat
    setup: setup.sh
    depends_on: []
    ssh_user: student
""",
        encoding="utf-8",
    )
    return load_definition(root / "lab.yaml")


def _image(store: ImageStore) -> None:
    blob = store.blobs / hashlib.sha256(b"image").hexdigest()
    blob.write_bytes(b"image")
    store.register_reference("local:test", blob.name, verified=True)


def _ownership_xml(uid: str, resource_type: str, vm_name: str | None = None) -> str:
    vm_field = f"<labctl:vm-name>{vm_name}</labctl:vm-name>" if vm_name is not None else ""
    return (
        '<labctl:ownership xmlns:labctl="urn:labctl">'
        f"<labctl:provider>kvm</labctl:provider><labctl:uid>{uid}</labctl:uid>"
        f"<labctl:resource-type>{resource_type}</labctl:resource-type>"
        f"{vm_field}"
        "</labctl:ownership>"
    )


def _write_state(tmp_path: Path, vms: dict[str, dict[str, object]]) -> Path:
    path = tmp_path / "state/labs/LX001.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "LX001",
                "provider": "kvm",
                "provider_uri": "qemu:///system",
                "instance_uid": "u1000-lx001",
                "network": "labctl-u1000-lx001-network",
                "state": "stopped",
                "vm_order": list(vms),
                "vms": vms,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_create_wires_proxy_user_data_without_secret_logs_or_state(tmp_path, monkeypatch, caplog):
    import yaml

    monkeypatch.setattr(
        os,
        "environ",
        {"http_proxy": "http://user:secret-token@proxy:3128", "TOKEN": "private-other"},
    )
    runner = FakeRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    orchestrator = KVMOrchestrator(
        tmp_path / "data", tmp_path / "state", runner=runner, keygen=_keys, host_keygen=_host_keys
    )
    with caplog.at_level("DEBUG"):
        state = orchestrator.create(
            _definition(tmp_path), "qemu:///system", images, readiness_probe=lambda *_: True
        )
    call = next(call for call in runner.calls if call[0] == "cloud-localds")
    user_data = Path(call[2])
    doc = yaml.safe_load(user_data.read_text())
    files = {entry["path"]: entry["content"] for entry in doc["write_files"]}
    assert (
        json.loads(files["/etc/labctl/proxy.json"])["http_proxy"]
        == "http://user:secret-token@proxy:3128"
    )
    proxy_values = json.loads(files["/etc/labctl/proxy.json"])
    network_xml = ElementTree.parse(tmp_path / "data/instances/LX001/network.xml")
    gateway = network_xml.find("ip").get("address")
    subnet = gateway.rsplit(".", 1)[0]
    assert "node" in proxy_values["no_proxy"].split(",")
    assert "node.lab" in proxy_values["no_proxy"].split(",")
    assert f"{subnet}.0/24" in proxy_values["no_proxy"].split(",")
    assert "10.0.0.0/8" not in proxy_values["no_proxy"]
    assert doc["runcmd"] == [["/usr/local/sbin/labctl-proxy-setup"]]
    assert "secret-token" not in caplog.text + json.dumps(state) + repr(runner.calls)
    assert "private-other" not in user_data.read_text()
    assert stat.S_IMODE(user_data.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(user_data.stat().st_mode) == 0o600


def test_create_uses_owned_isolated_resources_and_persists_digest(tmp_path: Path) -> None:
    runner = FakeRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        runner=runner,
        keygen=lambda directory: _keys(directory),
        host_keygen=lambda directory: _host_keys(directory),
    )
    state = orchestrator.create(
        _definition(tmp_path), "qemu:///session", images, readiness_probe=lambda *_: True
    )
    assert state["state"] == "ready"
    assert uuid.UUID(state["instance_uid"]).version == 4
    assert state["instance_uid"] != f"u{os.getuid()}-lx001"
    assert state["image_digests"]["local:test"] == hashlib.sha256(b"image").hexdigest()
    assert any(
        call[:4] == ("virsh", "--connect", "qemu:///session", "net-define") for call in runner.calls
    )
    overlay_create = next(call for call in runner.calls if call[:2] == ("qemu-img", "create"))
    assert overlay_create[-1] == str(8 * 1024**3)
    assert state["vms"]["node"]["disk_size_bytes"] == 8 * 1024**3
    assert any(call[0] == "virt-install" and "--noautoconsole" in call for call in runner.calls)
    assert all("shell=True" not in call for call in runner.calls)
    metadata_calls = [call for call in runner.calls if "metadata" in call and "--set" in call]
    metadata = metadata_calls[0][metadata_calls[0].index("--set") + 1]
    root = ElementTree.fromstring(metadata)
    assert root.tag == "{urn:labctl}ownership"
    assert root.findtext("{urn:labctl}provider") == "kvm"
    assert root.findtext("{urn:labctl}uid") == state["instance_uid"]
    assert root.findtext("{urn:labctl}resource-type") == "domain"
    assert root.findtext("{urn:labctl}vm-name") == "node"
    assert not metadata.lstrip().startswith("{")
    network_xml = ElementTree.parse(tmp_path / "data/instances/LX001/network.xml")
    assert (
        network_xml.findtext("metadata/{urn:labctl}ownership/{urn:labctl}uid")
        == state["instance_uid"]
    )


@pytest.mark.parametrize(
    "root",
    [
        "images=lab",
        "images,lab",
        r"images\lab",
        "images'lab",
        'images"lab',
        "images=lab,xpath.delete=./devices,quote'\\\"",
    ],
)
def test_prebuilt_disk_xml_preserves_literal_paths(tmp_path: Path, root: str) -> None:
    overlay = tmp_path / root / "disk.qcow2"
    seed = overlay.with_name("seed.iso")
    argv = KVMOrchestrator._prebuilt_disk_args(overlay, seed)
    assert argv[:2] == ["--disk", "none"]
    settings = {}
    for option in argv[3::2]:
        # virtinst.cli.parse_optstr_tuples uses POSIX shlex with comma
        # whitespace, then splits each token at its FIRST equals sign.
        lexer = shlex.shlex(option, posix=True)
        lexer.commenters = ""
        lexer.whitespace = ","
        lexer.whitespace_split = True
        fields = dict(token.split("=", 1) for token in lexer)
        if "xpath.create" in fields:
            assert fields == {"xpath.create": "./devices/disk[2]/readonly"}
            continue
        # Explicit value prevents XMLManualAction's LAST-equals splitting.
        assert set(fields) == {"xpath.set", "xpath.value"}
        settings[fields["xpath.set"]] = fields["xpath.value"]
    assert len(settings) == 14
    assert settings["./devices/disk[1]/source/@file"] == str(overlay)
    assert settings["./devices/disk[2]/source/@file"] == str(seed)


def test_create_attaches_prebuilt_disks_without_implicit_pool_management(tmp_path: Path) -> None:
    runner = FakeRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        runner=runner,
        keygen=_keys,
        host_keygen=_host_keys,
    )
    state = orchestrator.create(
        _definition(tmp_path), "qemu:///system", images, readiness_probe=lambda *_: True
    )
    argv = next(call for call in runner.calls if call[0] == "virt-install")
    # Both path= and source.file= invoke virtinst's implicit pool manager.
    assert [argv[i + 1] for i, arg in enumerate(argv) if arg == "--disk"] == ["none"]
    xml = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--xml"]
    vm = state["vms"]["node"]
    assert f"xpath.set=./devices/disk[1]/source/@file,xpath.value='{vm['overlay']}'" in xml
    assert "xpath.set=./devices/disk[1]/driver/@type,xpath.value='qcow2'" in xml
    assert "xpath.set=./devices/disk[1]/target/@bus,xpath.value='virtio'" in xml
    assert f"xpath.set=./devices/disk[2]/source/@file,xpath.value='{vm['seed']}'" in xml
    assert "xpath.set=./devices/disk[2]/@device,xpath.value='cdrom'" in xml
    assert "xpath.set=./devices/disk[2]/driver/@type,xpath.value='raw'" in xml
    assert "xpath.create=./devices/disk[2]/readonly" in xml
    assert list(argv) == vm["virt_install_argv"]
    assert not any(arg.startswith("pool-") for call in runner.calls for arg in call)


def test_create_splits_private_control_from_uuid_storage_and_copies_base(tmp_path: Path) -> None:
    runner = FakeRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    storage_root.chmod(0o2750)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        storage_root=storage_root,
        runner=runner,
        keygen=lambda directory: _keys(directory),
        host_keygen=lambda directory: _host_keys(directory),
    )

    state = orchestrator.create(
        _definition(tmp_path), "qemu:///system", images, readiness_probe=lambda *_: True
    )

    control = tmp_path / "data/instances/LX001"
    storage = storage_root / state["instance_uid"]
    digest = hashlib.sha256(b"image").hexdigest()
    copied_base = storage / "bases" / digest
    marker = json.loads((storage / ".labctl-owner.json").read_text(encoding="utf-8"))
    assert state["storage_path"] == str(storage)
    assert (control / "definition/lab.yaml").is_file()
    assert (control / "keys/id_lab").is_file()
    assert (control / "network.xml").is_file()
    assert (control / "vms/node/user-data").is_file()
    assert (control / "vms/node/known_hosts").is_file()
    assert state["vms"]["node"]["overlay"] == str(storage / "vms/node/disk.qcow2")
    assert state["vms"]["node"]["seed"] == str(storage / "vms/node/seed.iso")
    assert state["vms"]["node"]["backing_file"] == str(copied_base)
    assert copied_base.read_bytes() == b"image"
    assert marker == {
        "schema_version": 1,
        "provider": "kvm",
        "lab_id": "LX001",
        "instance_uid": state["instance_uid"],
        "resource_type": "storage",
    }
    assert not (storage / "keys").exists()
    assert not (storage / "vms/node/user-data").exists()


def test_create_refuses_preexisting_uuid_storage_without_touching_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
    storage_root = tmp_path / "storage"
    occupied = storage_root / str(expected)
    occupied.mkdir(parents=True)
    sentinel = occupied / "foreign"
    sentinel.write_text("keep", encoding="utf-8")
    images = ImageStore(tmp_path / "images", runner=FakeRunner())
    _image(images)
    orchestrator = KVMOrchestrator(
        tmp_path / "data", tmp_path / "state", storage_root=storage_root, runner=FakeRunner()
    )
    monkeypatch.setattr(orchestrator_module.uuid, "uuid4", lambda: expected)

    with pytest.raises(OrchestrationError, match="storage directory already exists"):
        orchestrator.create(_definition(tmp_path), "qemu:///system", images)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_split_create_rollback_removes_storage_then_private_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
    runner = FakeRunner(fail_on="virt-install")
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    storage_root.chmod(0o2750)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        storage_root=storage_root,
        runner=runner,
        keygen=lambda directory: _keys(directory),
        host_keygen=lambda directory: _host_keys(directory),
    )
    monkeypatch.setattr(orchestrator_module.uuid, "uuid4", lambda: expected)

    with pytest.raises(OrchestrationError, match="failed virt-install"):
        orchestrator.create(_definition(tmp_path), "qemu:///system", images)

    assert not (storage_root / str(expected)).exists()
    assert not (tmp_path / "data/instances/LX001").exists()
    assert not (tmp_path / "state/labs/LX001.json").exists()
    assert not (tmp_path / "state/transactions/LX001.json").exists()
    argv = next(call for call in runner.calls if call[0] == "virt-install")
    assert [argv[i + 1] for i, arg in enumerate(argv) if arg == "--disk"] == ["none"]
    assert not any(arg.startswith("pool-") for call in runner.calls for arg in call)


def test_new_layout_rejects_traversal_instance_identity(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    storage_root.chmod(0o2750)
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", storage_root=storage_root)
    state = {
        "id": "LX001",
        "instance_uid": "../victim",
        "storage_path": str(storage_root / "../victim"),
    }

    with pytest.raises(OrchestrationError, match="invalid persistent ownership identity"):
        orchestrator._storage_instance(state, marker=False)


def test_new_layout_storage_directories_ignore_private_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    storage_root.chmod(0o2750)
    shared_directory_chmods: list[Path] = []
    real_chmod = Path.chmod

    def observe_chmod(path: Path, mode: int, *args: object, **kwargs: object) -> None:
        if mode == 0o2750 and path.is_relative_to(storage_root):
            shared_directory_chmods.append(path)
        real_chmod(path, mode, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "chmod", observe_chmod)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        storage_root=storage_root,
        runner=runner,
        keygen=lambda directory: _keys(directory),
        host_keygen=lambda directory: _host_keys(directory),
    )
    previous_umask = os.umask(0o077)
    try:
        state = orchestrator.create(
            _definition(tmp_path), "qemu:///system", images, readiness_probe=lambda *_: True
        )
    finally:
        os.umask(previous_umask)

    storage = Path(state["storage_path"])
    assert storage.stat().st_mode & 0o7777 == 0o2750
    assert (storage / "bases").stat().st_mode & 0o7777 == 0o2750
    copied_base = next((storage / "bases").iterdir())
    assert copied_base.stat().st_mode & 0o777 == 0o444
    assert (storage / "vms").stat().st_mode & 0o7777 == 0o2750
    assert (storage / "vms/node").stat().st_mode & 0o7777 == 0o2750
    assert shared_directory_chmods == []


def test_directory_walk_allows_execute_only_intermediate_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intermediate = tmp_path / "execute-only"
    target = intermediate / "target"
    target.mkdir(parents=True)
    real_open = os.open

    def execute_only_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "execute-only" and flags & os.O_PATH == 0:
            raise PermissionError("intermediate directory is searchable but not readable")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", execute_only_open)

    with KVMOrchestrator._open_directory_no_symlinks(target) as descriptor:
        assert stat.S_ISDIR(os.fstat(descriptor).st_mode)


def test_domain_ownership_accepts_libvirt_uri_scoped_metadata_output() -> None:
    observed = """<ownership xmlns:labctl="urn:labctl">
  <provider>kvm</provider>
  <uid>550e8400-e29b-41d4-a716-446655440000</uid>
  <resource-type>domain</resource-type>
  <vm-name>node</vm-name>
</ownership>"""

    assert KVMOrchestrator._parse_ownership(observed) == {
        "provider": "kvm",
        "uid": "550e8400-e29b-41d4-a716-446655440000",
        "resource-type": "domain",
        "vm-name": "node",
    }


def test_create_does_not_read_storage_after_libvirt_takes_file_ownership(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"

    class OwnershipTakingRunner(FakeRunner):
        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            result = super().run(argv, check=check)
            if argv[0] == "virt-install":
                for pattern in ("*/bases/*", "*/vms/*/disk.qcow2", "*/vms/*/seed.iso"):
                    for path in storage_root.glob(pattern):
                        path.chmod(0)
            return result

    runner = OwnershipTakingRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    storage_root.mkdir()
    storage_root.chmod(0o2750)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        storage_root=storage_root,
        runner=runner,
        keygen=lambda directory: _keys(directory),
        host_keygen=lambda directory: _host_keys(directory),
    )

    state = orchestrator.create(
        _definition(tmp_path), "qemu:///system", images, readiness_probe=lambda *_: True
    )

    assert state["state"] == "ready"


def test_new_layout_rejects_configured_root_change_before_provider_io(tmp_path: Path) -> None:
    uid = "550e8400-e29b-41d4-a716-446655440000"
    original_root = tmp_path / "original"
    changed_root = tmp_path / "changed"
    changed_root.mkdir()
    storage = original_root / uid
    storage.mkdir(parents=True)
    (storage / ".labctl-owner.json").write_text("{}", encoding="utf-8")
    state_path = _write_state(tmp_path, {})
    state = load_state(state_path)
    state["instance_uid"] = uid
    state["storage_path"] = str(storage)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    runner = FakeRunner()
    orchestrator = KVMOrchestrator(
        tmp_path / "data", tmp_path / "state", storage_root=changed_root, runner=runner
    )

    with pytest.raises(OrchestrationError, match="configured storage root changed"):
        orchestrator.reconcile("LX001", repair=False)

    assert runner.calls == []


@pytest.mark.parametrize("tamper", ["contents", "symlink"])
def test_new_layout_marker_tamper_blocks_removal_before_provider_io(
    tmp_path: Path, tamper: str
) -> None:
    runner = FakeRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    storage_root.chmod(0o2750)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        storage_root=storage_root,
        runner=runner,
        keygen=lambda directory: _keys(directory),
        host_keygen=lambda directory: _host_keys(directory),
    )
    state = orchestrator.create(
        _definition(tmp_path), "qemu:///system", images, readiness_probe=lambda *_: True
    )
    storage = Path(state["storage_path"])
    marker = storage / ".labctl-owner.json"
    marker.unlink()
    if tamper == "contents":
        marker.write_text("{}", encoding="utf-8")
        marker.chmod(0o600)
    else:
        target = tmp_path / "foreign-marker"
        target.write_text("{}", encoding="utf-8")
        marker.symlink_to(target)
    runner.calls.clear()

    with pytest.raises(OrchestrationError, match="storage ownership marker"):
        orchestrator.remove("LX001", force=True)

    assert runner.calls == []
    assert storage.exists()
    assert (tmp_path / "data/instances/LX001").exists()


def test_new_layout_removal_deletes_storage_before_private_state(tmp_path: Path) -> None:
    runner = FakeRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    storage_root.chmod(0o2750)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        storage_root=storage_root,
        runner=runner,
        keygen=lambda directory: _keys(directory),
        host_keygen=lambda directory: _host_keys(directory),
    )
    state = orchestrator.create(
        _definition(tmp_path), "qemu:///system", images, readiness_probe=lambda *_: True
    )
    storage = Path(state["storage_path"])
    control = tmp_path / "data/instances/LX001"

    orchestrator.remove("LX001", force=True)

    assert not storage.exists()
    assert not control.exists()
    assert not (tmp_path / "state/labs/LX001.json").exists()


def test_new_layout_remove_recovers_when_storage_was_deleted_before_stage_save(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    storage_root.chmod(0o2750)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        storage_root=storage_root,
        runner=runner,
        keygen=lambda directory: _keys(directory),
        host_keygen=lambda directory: _host_keys(directory),
    )
    state = orchestrator.create(
        _definition(tmp_path), "qemu:///system", images, readiness_probe=lambda *_: True
    )
    storage = Path(state["storage_path"])
    shutil.rmtree(storage)
    journal_path = tmp_path / "state/transactions/LX001.json"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "LX001",
                "operation": "remove",
                "provider_uri": state["provider_uri"],
                "instance_uid": state["instance_uid"],
                "network": state["network"],
                "vm_order": state["vm_order"],
                "domains": {"node": "planned"},
                "network_stage": "planned",
                "instance_stage": "planned",
                "storage_path": str(storage),
                "storage_stage": "planned",
            }
        ),
        encoding="utf-8",
    )

    orchestrator.remove("LX001", force=True)

    assert not (tmp_path / "state/labs/LX001.json").exists()
    assert not journal_path.exists()


def test_new_layout_reset_backing_uses_and_verifies_copied_base(tmp_path: Path) -> None:
    uid = "550e8400-e29b-41d4-a716-446655440000"
    digest = hashlib.sha256(b"base").hexdigest()
    storage_root = tmp_path / "storage"
    storage = storage_root / uid
    base = storage / "bases" / digest
    base.parent.mkdir(parents=True)
    base.write_bytes(b"base")
    KVMOrchestrator._write_storage_marker(storage, "LX001", uid)
    state = {
        "schema_version": 1,
        "id": "LX001",
        "provider": "kvm",
        "provider_uri": "qemu:///system",
        "instance_uid": uid,
        "storage_path": str(storage),
    }
    vm = {"image_digest": digest, "backing_file": str(base)}
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", storage_root=storage_root)

    assert orchestrator._validated_instance_backing(state, vm) == base
    base.write_bytes(b"tampered")
    with pytest.raises(OrchestrationError, match="checksum mismatch"):
        orchestrator._validated_instance_backing(state, vm)


def _keys(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    private = directory / "id_lab"
    public = directory / "id_lab.pub"
    private.write_text("private fixture", encoding="utf-8")
    public.write_text("ssh-ed25519 AAAA fixture", encoding="utf-8")
    private.chmod(0o600)
    public.chmod(0o644)
    return private, public


def _host_keys(directory: Path) -> tuple[Path, Path]:
    private, public = _keys(directory)
    host_private = directory / "ssh_host_ed25519_key"
    host_public = directory / "ssh_host_ed25519_key.pub"
    private.rename(host_private)
    public.rename(host_public)
    return host_private, host_public


def test_create_rolls_back_resources_and_state_on_failure(tmp_path: Path) -> None:
    runner = FakeRunner(fail_on="virt-install")
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        runner=runner,
        keygen=lambda directory: _keys(directory),
        host_keygen=lambda directory: _host_keys(directory),
    )
    with pytest.raises(OrchestrationError, match="failed virt-install"):
        orchestrator.create(_definition(tmp_path), "qemu:///session", images)
    assert not (tmp_path / "state/labs/LX001.json").exists()
    assert any("net-undefine" in call for call in runner.calls)


def test_create_crash_journal_is_recovered_on_retry(tmp_path: Path) -> None:
    class CrashRunner(FakeRunner):
        crash = True

        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            if argv[0] == "virt-install" and self.crash:
                self.crash = False
                raise KeyboardInterrupt
            return super().run(argv, check=check)

    runner = CrashRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    definition = _definition(tmp_path)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        runner=runner,
        keygen=lambda directory: _keys(directory),
        host_keygen=lambda directory: _host_keys(directory),
    )

    with pytest.raises(KeyboardInterrupt):
        orchestrator.create(definition, "qemu:///session", images)

    journal = tmp_path / "state/transactions/LX001.json"
    transaction = json.loads(journal.read_text(encoding="utf-8"))
    assert transaction["operation"] == "create"
    assert transaction["vms"]["node"]["stage"] == "virt-install-planned"

    state = orchestrator.create(
        definition, "qemu:///session", images, readiness_probe=lambda *_: True
    )

    assert state["state"] == "ready"
    assert not journal.exists()


@pytest.mark.parametrize("split_storage", [False, True])
@pytest.mark.parametrize("interruption", ["virt-install", "readiness"])
def test_remove_recovers_interrupted_create(
    tmp_path: Path, split_storage: bool, interruption: str
) -> None:
    class InterruptRunner(FakeRunner):
        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            if argv[0] == "virt-install" and interruption == "virt-install":
                raise KeyboardInterrupt
            return super().run(argv, check=check)

    runner = InterruptRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    storage = tmp_path / "storage" if split_storage else None
    if storage is not None:
        storage.mkdir()
        storage.chmod(0o2750)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        runner=runner,
        keygen=_keys,
        host_keygen=_host_keys,
        storage_root=storage,
    )

    def interrupt(*_: object) -> bool:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        orchestrator.create(
            _definition(tmp_path), "qemu:///system", images, readiness_probe=interrupt
        )
    state = load_state(tmp_path / "state/labs/LX001.json")
    runner.calls.clear()
    orchestrator.remove("LX001", force=False)

    assert not (tmp_path / "state/labs/LX001.json").exists()
    assert not (tmp_path / "state/transactions/LX001.json").exists()
    assert not (tmp_path / "data/instances/LX001").exists()
    if storage is not None:
        assert not Path(state["storage_path"]).exists()
    assert not any(
        call[0] in {"virt-install", "qemu-img", "cloud-localds"} for call in runner.calls
    )


def _pending_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, committed: bool = False):  # type: ignore[no-untyped-def]
    runner = FakeRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        runner=runner,
        keygen=_keys,
        host_keygen=_host_keys,
    )
    journal = tmp_path / "state/transactions/LX001.json"

    def interrupt(*_: object) -> bool:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        if committed:
            patch.setattr(orchestrator, "_unlink_durable", interrupt)
        with pytest.raises(KeyboardInterrupt):
            orchestrator.create(
                _definition(tmp_path),
                "qemu:///system",
                images,
                readiness_probe=(lambda *_: True) if committed else interrupt,
            )
    state = load_state(tmp_path / "state/labs/LX001.json")
    return orchestrator, runner, images, journal, state


@pytest.mark.parametrize("force", [False, True])
def test_remove_finishes_committed_create_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    orchestrator, runner, _images, journal, state = _pending_create(
        tmp_path, monkeypatch, committed=True
    )
    assert state["state"] == "ready"
    runner.calls.clear()
    orchestrator.remove("LX001", force=force)
    assert not journal.exists()
    assert not (tmp_path / "state/labs/LX001.json").exists()
    assert not (tmp_path / "data/instances/LX001").exists()
    assert any("dominfo" in call for call in runner.calls)


class CreatedResourceRunner(FakeRunner):
    """Model already-created resources without touching libvirt."""

    def __init__(self, uid: str, *, failure: str = "") -> None:
        super().__init__()
        self.uid = uid
        self.failure = failure
        self.domain_exists = True
        self.network_exists = True
        self.running = True

    def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
        call = tuple(argv)
        self.calls.append(call)
        command = argv[3]
        resource = argv[4]
        if command == "dominfo" and self.failure == "probe":
            return CommandResult(call, 1, "", "permission denied")
        if command in {"dominfo", "net-info"}:
            domain = command == "dominfo"
            exists = self.domain_exists if domain else self.network_exists
            kind = "domain" if domain else "network"
            return CommandResult(
                call,
                0 if exists else 1,
                "Active: yes\n" if exists else "",
                "" if exists else f"failed to get {kind} '{resource}'",
            )
        if command == "metadata":
            uid = "foreign" if self.failure == "foreign-domain" else self.uid
            return CommandResult(call, 0, _ownership_xml(uid, "domain", "node"), "")
        if command == "net-dumpxml":
            uid = "foreign" if self.failure == "foreign-network" else self.uid
            return CommandResult(
                call,
                0,
                "<network><metadata>" + _ownership_xml(uid, "network") + "</metadata></network>",
                "",
            )
        if command == "domstate":
            return CommandResult(call, 0, "running" if self.running else "shut off", "")
        if command == "net-destroy" and self.failure == "destroy":
            raise RuntimeError("network destroy failed")
        if command in {"destroy", "shutdown"}:
            self.running = False
        if command == "undefine":
            self.domain_exists = False
        if command == "net-undefine":
            self.network_exists = False
        return CommandResult(call, 0, "", "")


@pytest.mark.parametrize("failure", ["probe", "foreign-domain", "foreign-network", "destroy"])
def test_remove_create_recovery_failure_preserves_journal_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    orchestrator, _runner, _images, journal, state = _pending_create(tmp_path, monkeypatch)
    runner = CreatedResourceRunner(state["instance_uid"], failure=failure)
    orchestrator.runner = runner
    before = journal.read_bytes()
    with pytest.raises(OrchestrationError, match="create recovery failed"):
        orchestrator.remove("LX001", force=True)
    assert journal.read_bytes() == before
    assert (tmp_path / "state/labs/LX001.json").exists()
    assert (tmp_path / "data/instances/LX001").exists()
    if failure in {"probe", "foreign-domain"}:
        assert runner.domain_exists
        assert not any(call[3] in {"destroy", "undefine"} for call in runner.calls)
    if failure == "foreign-network":
        assert runner.network_exists
        assert not any(call[3] in {"net-destroy", "net-undefine"} for call in runner.calls)
    # Removing the simulated fault permits retry; already-absent resources are accepted.
    runner.failure = ""
    orchestrator.remove("LX001", force=False)
    assert not runner.domain_exists and not runner.network_exists
    assert not journal.exists()
    assert not (tmp_path / "state/labs/LX001.json").exists()
    assert not (tmp_path / "data/instances/LX001").exists()
    assert len([call for call in runner.calls if call[3] == "undefine"]) == 1


def test_remove_committed_create_preserves_running_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, _runner, _images, journal, state = _pending_create(
        tmp_path, monkeypatch, committed=True
    )
    runner = CreatedResourceRunner(state["instance_uid"])
    orchestrator.runner = runner
    with pytest.raises(OrchestrationError, match="lab is running"):
        orchestrator.remove("LX001", force=False)
    assert not journal.exists()  # Commit acknowledgement, not rollback.
    assert load_state(tmp_path / "state/labs/LX001.json") == state
    assert runner.domain_exists and runner.network_exists
    assert not any(
        call[3] in {"destroy", "undefine", "net-destroy", "net-undefine"} for call in runner.calls
    )
    orchestrator.remove("LX001", force=True)
    assert not runner.domain_exists and not runner.network_exists
    assert not (tmp_path / "state/labs/LX001.json").exists()


def test_remove_create_recovery_holds_lifecycle_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, runner, _images, journal, _state = _pending_create(tmp_path, monkeypatch)
    runner.calls.clear()
    before = journal.read_bytes()
    with (
        file_lock(orchestrator.lock_root / "lab-LX001.lock"),
        pytest.raises(OrchestrationError, match="lock"),
    ):
        orchestrator.remove("LX001", force=True)
    assert journal.read_bytes() == before
    assert runner.calls == []
    recover = orchestrator._recover_create

    def check_lock(transaction):  # type: ignore[no-untyped-def]
        with (
            pytest.raises(OrchestrationError, match="lock"),
            orchestrator._lab_lock("LX001"),
        ):
            pytest.fail("recovery must run under the lifecycle lock")
        recover(transaction)

    monkeypatch.setattr(orchestrator, "_recover_create", check_lock)
    orchestrator.remove("LX001", force=False)
    assert not journal.exists()


def test_reset_refuses_real_pending_create_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, runner, images, journal, state = _pending_create(tmp_path, monkeypatch)
    before = journal.read_bytes()
    runner.calls.clear()
    with pytest.raises(OrchestrationError, match=r"pending transaction \(create\)"):
        orchestrator.reset("LX001", images)
    assert journal.read_bytes() == before
    assert load_state(tmp_path / "state/labs/LX001.json") == state
    assert runner.calls == []


def test_create_fsyncs_generated_artifacts_before_ready_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        runner=runner,
        keygen=lambda directory: _keys(directory),
        host_keygen=lambda directory: _host_keys(directory),
    )
    synced: set[Path] = set()
    real_fsync = os.fsync
    real_save_state = orchestrator_module.save_state

    def observe_fsync(descriptor: int) -> None:
        synced.add(Path(os.readlink(f"/proc/self/fd/{descriptor}")))
        real_fsync(descriptor)

    def assert_artifacts_synced(path: Path, state: dict[str, object]) -> None:
        if state.get("state") == "ready":
            instance = tmp_path / "data/instances/LX001"
            assert instance / "network.xml" in synced
            assert instance / "vms/node/user-data" in synced
            assert instance / "vms/node/meta-data" in synced
            assert instance / "vms/node" in synced
            assert instance in synced
        real_save_state(path, state)

    monkeypatch.setattr(os, "fsync", observe_fsync)
    monkeypatch.setattr(orchestrator_module, "save_state", assert_artifacts_synced)

    orchestrator.create(
        _definition(tmp_path), "qemu:///session", images, readiness_probe=lambda *_: True
    )


def test_create_recovery_fsyncs_storage_parent_before_metadata_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CrashRunner(FakeRunner):
        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            if argv[0] == "virt-install":
                raise KeyboardInterrupt
            return super().run(argv, check=check)

    runner = CrashRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        runner=runner,
        keygen=lambda directory: _keys(directory),
        host_keygen=lambda directory: _host_keys(directory),
    )
    with pytest.raises(KeyboardInterrupt):
        orchestrator.create(_definition(tmp_path), "qemu:///session", images)
    journal_path = tmp_path / "state/transactions/LX001.json"
    transaction = json.loads(journal_path.read_text(encoding="utf-8"))
    instance = tmp_path / "data/instances/LX001"
    storage_parent = instance.parent
    removed = False
    synced_after_removal: set[Path] = set()
    real_rmtree = shutil.rmtree
    real_fsync = os.fsync
    real_unlink = Path.unlink

    def observe_rmtree(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal removed
        real_rmtree(path, *args, **kwargs)
        if path == instance:
            removed = True

    def observe_fsync(descriptor: int) -> None:
        if removed:
            synced_after_removal.add(Path(os.readlink(f"/proc/self/fd/{descriptor}")))
        real_fsync(descriptor)

    def require_storage_sync(path: Path, *args: object, **kwargs: object) -> None:
        if path in {tmp_path / "state/labs/LX001.json", journal_path}:
            assert storage_parent in synced_after_removal
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", observe_rmtree)
    monkeypatch.setattr(os, "fsync", observe_fsync)
    monkeypatch.setattr(Path, "unlink", require_storage_sync)

    orchestrator._recover_create(transaction)


def test_split_create_recovery_before_storage_creation_removes_bound_metadata(
    tmp_path: Path,
) -> None:
    uid = "550e8400-e29b-41d4-a716-446655440000"
    storage_root = tmp_path / "libvirt"
    storage_root.mkdir()
    storage_root.chmod(0o2750)
    instance = tmp_path / "data/instances/LX001"
    instance.mkdir(parents=True)
    state_path = tmp_path / "state/labs/LX001.json"
    state_path.parent.mkdir(parents=True)
    storage = storage_root / uid
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "LX001",
                "provider": "kvm",
                "provider_uri": "qemu:///system",
                "state": "provisioning",
                "instance_uid": uid,
                "network": f"labctl-{uid}-network",
                "vm_order": [],
                "vms": {},
                "storage_path": str(storage),
            }
        ),
        encoding="utf-8",
    )
    journal_path = tmp_path / "state/transactions/LX001.json"
    journal_path.parent.mkdir(parents=True)
    journal = {
        "schema_version": 1,
        "id": "LX001",
        "operation": "create",
        "provider_uri": "qemu:///system",
        "instance_uid": uid,
        "instance_owned": True,
        "storage_path": str(storage),
        "storage_owned": True,
        "network": f"labctl-{uid}-network",
        "vm_order": [],
        "vms": {},
    }
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        runner=FakeRunner(),
        storage_root=storage_root,
    )

    orchestrator._recover_create(journal)

    assert not storage.exists()
    assert not instance.exists()
    assert not state_path.exists()
    assert not journal_path.exists()


def test_create_persists_random_identity_before_first_provider_mutation(tmp_path: Path) -> None:
    expected = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")

    class PersistenceRunner(FakeRunner):
        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            if "net-define" in argv:
                state = load_state(tmp_path / "state/labs/LX001.json")
                journal = json.loads(
                    (tmp_path / "state/transactions/LX001.json").read_text(encoding="utf-8")
                )
                assert state["instance_uid"] == str(expected)
                assert journal["instance_uid"] == str(expected)
            return super().run(argv, check=check)

    runner = PersistenceRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        runner=runner,
        keygen=lambda directory: _keys(directory),
        host_keygen=lambda directory: _host_keys(directory),
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(orchestrator_module.uuid, "uuid4", lambda: expected)
        state = orchestrator.create(
            _definition(tmp_path), "qemu:///session", images, readiness_probe=lambda *_: True
        )

    assert state["network"] == f"labctl-{expected}-network"
    assert state["vms"]["node"]["domain"] == f"labctl-{expected}-node"


@pytest.mark.parametrize("tamper", ["uid", "network", "domain", "overlay", "stage"])
def test_create_recovery_validates_complete_journal_before_provider_io(
    tmp_path: Path, tamper: str
) -> None:
    instance = tmp_path / "data/instances/LX001"
    vm_dir = instance / "vms/node"
    vm_dir.mkdir(parents=True)
    overlay = vm_dir / "disk.qcow2"
    seed = vm_dir / "seed.iso"
    overlay.write_bytes(b"overlay")
    seed.write_bytes(b"seed")
    uid = "550e8400-e29b-41d4-a716-446655440000"
    domain = f"labctl-{uid}-node"
    network = f"labctl-{uid}-network"
    vm = _vm(domain, tmp_path, "provisioning")
    argv = [
        "virt-install",
        "--connect",
        "qemu:///session",
        "--name",
        domain,
        "--memory",
        "1024",
        "--vcpus",
        "1",
        "--import",
        "--disk",
        f"path={overlay},format=qcow2",
        "--disk",
        f"path={seed},device=cdrom",
        "--network",
        f"network={network},model=virtio",
        "--osinfo",
        "detect=on,require=off",
        "--noautoconsole",
        "--wait",
        "0",
    ]
    vm.update({"overlay": str(overlay), "seed": str(seed), "virt_install_argv": argv})
    state_path = _write_state(tmp_path, {"node": vm})
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        provider_uri="qemu:///session",
        instance_uid=uid,
        network=network,
        state="provisioning",
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    journal: dict[str, object] = {
        "schema_version": 1,
        "id": "LX001",
        "operation": "create",
        "provider_uri": "qemu:///session",
        "instance_uid": uid,
        "instance_owned": True,
        "network": network,
        "vm_order": ["node"],
        "vms": {
            "node": {
                "domain": domain,
                "stage": "virt-install-planned",
                "material": [
                    domain,
                    1024 * 1024**2,
                    1,
                    [["disk", str(overlay)], ["cdrom", str(seed)]],
                    "network",
                    network,
                    "virtio",
                ],
            }
        },
    }
    record = journal["vms"]["node"]  # type: ignore[index]
    assert isinstance(record, dict)
    if tamper == "uid":
        journal["instance_uid"] = "u0-lx001"
    elif tamper == "network":
        journal["network"] = "foreign"
    elif tamper == "domain":
        record["domain"] = "foreign"
    elif tamper == "overlay":
        record["material"][3][0][1] = str(tmp_path / "victim")  # type: ignore[index]
    else:
        record["stage"] = "unknown"
    runner = FakeRunner()
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError, match="invalid create transaction"):
        orchestrator._recover_create(journal)

    assert runner.calls == []
    assert instance.exists()
    assert state_path.exists()


def test_created_domain_cleanup_never_requests_all_attached_storage(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    runner = CleanupRunner({domain})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    orchestrator._cleanup_created_domain("qemu:///system", domain, "u1000-lx001", "node")

    undefine = next(call for call in runner.calls if call[3] == "undefine")
    assert "--nvram" in undefine
    assert "--remove-all-storage" not in undefine


def test_create_recovery_without_bound_state_performs_no_provider_io(tmp_path: Path) -> None:
    uid = "550e8400-e29b-41d4-a716-446655440000"
    runner = FakeRunner()
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    journal = {
        "schema_version": 1,
        "id": "LX001",
        "operation": "create",
        "provider_uri": "qemu:///system",
        "instance_uid": uid,
        "instance_owned": True,
        "network": f"labctl-{uid}-network",
        "vm_order": [],
        "vms": {},
    }

    orchestrator._recover_create(journal)

    assert runner.calls == []


def test_create_recovery_retains_metadata_when_network_destroy_fails(
    tmp_path: Path,
) -> None:
    uid = "550e8400-e29b-41d4-a716-446655440000"
    network = f"labctl-{uid}-network"
    state_path = _write_state(tmp_path, {})
    state = load_state(state_path)
    state.update(
        provider_uri="qemu:///session",
        instance_uid=uid,
        network=network,
        state="provisioning",
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    instance = tmp_path / "data/instances/LX001"
    instance.mkdir(parents=True)
    journal_path = tmp_path / "state/transactions/LX001.json"
    journal_path.parent.mkdir(parents=True)
    journal = {
        "schema_version": 1,
        "id": "LX001",
        "operation": "create",
        "provider_uri": "qemu:///session",
        "instance_uid": uid,
        "instance_owned": True,
        "network": network,
        "vm_order": [],
        "vms": {},
    }
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    class TransientNetworkRunner(FakeRunner):
        defined = True
        active = True

        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            call = tuple(argv)
            self.calls.append(call)
            command = argv[3]
            if command == "net-info":
                exists = self.defined or self.active
                error = "" if exists else f"failed to get network '{network}'"
                output = f"Active: {'yes' if self.active else 'no'}\n" if exists else ""
                return CommandResult(call, 0 if exists else 1, output, error)
            if command == "net-dumpxml":
                xml = (
                    "<network><metadata>" + _ownership_xml(uid, "network") + "</metadata></network>"
                )
                return CommandResult(call, 0, xml, "")
            if command == "net-destroy":
                result = CommandResult(call, 1, "", "network is still active")
                if check:
                    raise RuntimeError(result.stderr)
                return result
            if command == "net-undefine":
                self.defined = False
                return CommandResult(call, 0, "", "")
            return super().run(argv, check=check)

    runner = TransientNetworkRunner()
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError, match="network is still active"):
        orchestrator._recover_create(journal)

    assert runner.active
    assert state_path.exists()
    assert journal_path.exists()
    assert instance.exists()


def test_create_recovery_accepts_successful_transient_network_destruction(tmp_path: Path) -> None:
    uid = "550e8400-e29b-41d4-a716-446655440000"
    network = f"labctl-{uid}-network"
    state_path = _write_state(tmp_path, {})
    state = load_state(state_path)
    state.update(
        provider_uri="qemu:///session",
        instance_uid=uid,
        network=network,
        state="provisioning",
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    instance = tmp_path / "data/instances/LX001"
    instance.mkdir(parents=True)
    journal_path = tmp_path / "state/transactions/LX001.json"
    journal_path.parent.mkdir(parents=True)
    journal = {
        "schema_version": 1,
        "id": "LX001",
        "operation": "create",
        "provider_uri": "qemu:///session",
        "instance_uid": uid,
        "instance_owned": True,
        "network": network,
        "vm_order": [],
        "vms": {},
    }
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    class TransientNetworkRunner(FakeRunner):
        active = True

        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            call = tuple(argv)
            self.calls.append(call)
            command = argv[3]
            if command == "net-info":
                if not self.active:
                    return CommandResult(call, 1, "", f"failed to get network '{network}'")
                return CommandResult(call, 0, "Active: yes\n", "")
            if command == "net-dumpxml":
                if not self.active:
                    return CommandResult(call, 1, "", f"failed to get network '{network}'")
                xml = (
                    "<network><metadata>" + _ownership_xml(uid, "network") + "</metadata></network>"
                )
                return CommandResult(call, 0, xml, "")
            if command == "net-destroy":
                self.active = False
                return CommandResult(call, 0, "", "")
            return super().run(argv, check=check)

    orchestrator = KVMOrchestrator(
        tmp_path / "data", tmp_path / "state", runner=TransientNetworkRunner()
    )

    orchestrator._recover_create(journal)

    assert not state_path.exists()
    assert not journal_path.exists()
    assert not instance.exists()


def test_create_rollback_never_removes_preexisting_instance_directory(tmp_path: Path) -> None:
    instance = tmp_path / "data/instances/LX001"
    instance.mkdir(parents=True)
    marker = instance / "preexisting"
    marker.write_text("keep", encoding="utf-8")
    runner = FakeRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError):
        orchestrator.create(_definition(tmp_path), "qemu:///session", images)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "state/labs/LX001.json").exists()


def test_untrusted_image_rejected_before_resource_commands(tmp_path: Path) -> None:
    runner = FakeRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    digest = hashlib.sha256(b"image").hexdigest()
    (images.blobs / digest).write_bytes(b"image")
    images.register_reference("local:test", digest, verified=False)
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    with pytest.raises(OrchestrationError, match="untrusted"):
        orchestrator.create(_definition(tmp_path), "qemu:///session", images)
    assert runner.calls == []


def test_stop_graceful_then_force_and_uri_is_bound(tmp_path: Path) -> None:
    domain = "labctl-u-LX001-node"
    runner = StateRunner({domain: "running"})
    state_path = tmp_path / "state/labs/LX001.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "LX001",
                "provider_uri": "qemu:///system",
                "state": "running",
                "instance_uid": "u1000-lx001",
                "vm_order": ["node"],
                "vms": {"node": {"domain": domain, "state": "running"}},
            }
        ),
        encoding="utf-8",
    )
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    orchestrator.stop("LX001", force=True, wait_stopped=lambda *_: False)
    commands = [
        call[3]
        for call in runner.calls
        if call[:3] == ("virsh", "--connect", "qemu:///system")
        and call[3] in {"shutdown", "destroy"}
    ]
    assert commands == ["shutdown", "destroy"]


class StateRunner:
    def __init__(
        self,
        power: dict[str, str],
        *,
        fail_start: str | None = None,
        fail_destroy: str | None = None,
        network_xml: str | None = None,
    ) -> None:
        self.power = power
        self.fail_start = fail_start
        self.fail_destroy = fail_destroy
        self.network_xml = network_xml
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
        call = tuple(argv)
        self.calls.append(call)
        command = argv[3] if argv[0] == "virsh" else argv[0]
        resource = argv[4] if len(argv) > 4 else ""
        if command == "metadata":
            name = resource.rsplit("-", 1)[-1]
            return CommandResult(call, 0, _ownership_xml("u1000-lx001", "domain", name), "")
        if command == "net-dumpxml":
            output = self.network_xml or (
                "<network><metadata>"
                + _ownership_xml("u1000-lx001", "network")
                + "</metadata></network>"
            )
            return CommandResult(call, 0, output, "")
        if command in {"dominfo", "net-info"}:
            return CommandResult(call, 0, "exists", "")
        if command == "domstate":
            return CommandResult(call, 0, self.power[resource] + "\n", "")
        if command == "domifaddr":
            return CommandResult(call, 0, " vnet0  52:54:00:00:00:01  ipv4  192.0.2.10/24\n", "")
        if command == "start":
            if resource == self.fail_start:
                raise RuntimeError(f"failed start {resource}")
            self.power[resource] = "running"
        elif command in {"shutdown", "destroy"}:
            if command == "destroy" and resource == self.fail_destroy:
                raise RuntimeError(f"failed restore {resource}")
            self.power[resource] = "shut off"
        return CommandResult(call, 0, "", "")


class CleanupRunner:
    def __init__(self, domains: set[str], *, network_exists: bool = True) -> None:
        self.domains = domains
        self.network_exists = network_exists
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
        call = tuple(argv)
        self.calls.append(call)
        command = argv[3]
        resource = argv[4]
        if command == "metadata":
            name = resource.rsplit("-", 1)[-1]
            output = (
                _ownership_xml("u1000-lx001", "domain", name) if resource in self.domains else ""
            )
            return CommandResult(call, 0 if output else 1, output, "")
        if command == "net-dumpxml":
            output = (
                "<network><metadata>"
                + _ownership_xml("u1000-lx001", "network")
                + "</metadata></network>"
                if self.network_exists
                else ""
            )
            return CommandResult(call, 0 if output else 1, output, "")
        if command == "net-info":
            error = "" if self.network_exists else f"failed to get network '{resource}'"
            return CommandResult(call, 0 if self.network_exists else 1, "", error)
        if command == "dominfo":
            error = "" if resource in self.domains else f"failed to get domain '{resource}'"
            return CommandResult(call, 0 if resource in self.domains else 1, "", error)
        if command == "domstate":
            return CommandResult(call, 0, "shut off\n", "")
        if command == "undefine":
            self.domains.remove(resource)
        if command == "net-undefine":
            self.network_exists = False
        return CommandResult(call, 0, "", "")


def _vm(
    domain: str,
    root: Path,
    state: str = "stopped",
    depends_on: list[str] | None = None,
) -> dict[str, object]:
    return {
        "domain": domain,
        "state": state,
        "depends_on": depends_on or [],
        "host_public_key": "ssh-ed25519 AAAA fixture",
        "known_hosts": str(root / "known-hosts"),
        "identity_file": str(root / "key"),
        "ssh_user": "student",
    }


def _single_reset_fixture(
    tmp_path: Path,
) -> tuple[str, Path, ImageStore, dict[str, dict[str, object]]]:
    uid = "u1000-lx001"
    domain = f"labctl-{uid}-node"
    network = f"labctl-{uid}-network"
    vm_dir = tmp_path / "data/instances/LX001/vms/node"
    vm_dir.mkdir(parents=True)
    overlay = vm_dir / "disk.qcow2"
    seed = vm_dir / "seed.iso"
    overlay.write_bytes(b"original overlay")
    seed.write_bytes(b"seed")
    digest = hashlib.sha256(b"base").hexdigest()
    vm = _vm(domain, tmp_path, "ready")
    vm.update(
        {
            "overlay": str(overlay),
            "seed": str(seed),
            "disk_size_bytes": 8 * 1024**3,
            "image_digest": digest,
            "virt_install_argv": [
                "virt-install",
                "--connect",
                "qemu:///system",
                "--name",
                domain,
                "--memory",
                "1024",
                "--vcpus",
                "1",
                "--import",
                "--disk",
                f"path={overlay},format=qcow2",
                "--disk",
                f"path={seed},device=cdrom",
                "--network",
                f"network={network},model=virtio",
                "--osinfo",
                "detect=on,require=off",
                "--noautoconsole",
                "--wait",
                "0",
            ],
        }
    )
    _write_state(tmp_path, {"node": vm})
    images = ImageStore(tmp_path / "images")
    (images.blobs / digest).write_bytes(b"base")
    return domain, overlay, images, {"node": vm}


class ResetFileRunner(StateRunner):
    def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
        if argv[0] == "qemu-img":
            self.calls.append(tuple(argv))
            Path(argv[-2]).write_bytes(b"replacement overlay")
            return CommandResult(tuple(argv), 0, "", "")
        if argv[0] == "virt-install":
            self.calls.append(tuple(argv))
            self.power[argv[argv.index("--name") + 1]] = "running"
            return CommandResult(tuple(argv), 0, "", "")
        return super().run(argv, check=check)


@pytest.mark.parametrize("selected", [None, ("node",)])
def test_reset_reuses_creation_seed_without_recapturing_host_proxy(tmp_path, monkeypatch, selected):
    from labctl.cloudinit import generate_cloud_init

    domain, overlay, images, _ = _single_reset_fixture(tmp_path)
    monkeypatch.setattr(os, "environ", {"https_proxy": "http://creation-proxy:3128"})
    original = generate_cloud_init("ssh-ed25519 AAAA lab", ["true"]).encode()
    user_data = overlay.with_name("user-data")
    seed = overlay.with_name("seed.iso")
    user_data.write_bytes(original)
    seed.write_bytes(original)
    monkeypatch.setattr(os, "environ", {"https_proxy": "http://changed-proxy:3128"})
    runner = ResetFileRunner({domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    orchestrator.reset("LX001", images, vm_names=selected, readiness_probe=lambda *_: True)
    assert seed.read_bytes() == user_data.read_bytes() == original
    assert not any(call[0] == "cloud-localds" for call in runner.calls)
    assert any(str(seed) in arg for call in runner.calls for arg in call)


@pytest.mark.parametrize("record_format", ["legacy", "shorthand", "explicit"])
def test_reset_attaches_prebuilt_disks_without_implicit_pool_management(
    tmp_path: Path, record_format: str
) -> None:
    domain, overlay, images, _ = _single_reset_fixture(tmp_path)
    runner = ResetFileRunner({domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    path = tmp_path / "state/labs/LX001.json"
    if record_format != "legacy":
        state = load_state(path)
        argv = state["vms"]["node"]["virt_install_argv"]
        disks = orchestrator._prebuilt_disk_args(overlay, overlay.with_name("seed.iso"))
        if record_format == "shorthand":
            disks = [
                shlex.split(item)[0].removeprefix("xpath.set=").replace(",xpath.value=", "=", 1)
                if item.startswith("xpath.set=")
                else item
                for item in disks
            ]
        argv[10:14] = disks
        path.write_text(json.dumps(state), encoding="utf-8")
    orchestrator.reset("LX001", images, readiness_probe=lambda *_: True)
    installs = [call for call in runner.calls if call[0] == "virt-install"]
    assert installs
    for argv in installs:
        assert [argv[i + 1] for i, arg in enumerate(argv) if arg == "--disk"] == ["none"]
        assert list(argv[10 : argv.index("--network")]) == orchestrator._prebuilt_disk_args(
            overlay, overlay.with_name("seed.iso")
        )
    assert not any(arg.startswith("pool-") for call in runner.calls for arg in call)


@pytest.mark.parametrize("record_format", ["legacy", "shorthand", "explicit"])
@pytest.mark.parametrize("tamper", ["foreign-disk", "pool", "extra-xml", "uri", "memory"])
def test_reset_rejects_tampered_disk_records_before_resource_io(
    tmp_path: Path, record_format: str, tamper: str
) -> None:
    domain, overlay, images, _ = _single_reset_fixture(tmp_path)
    runner = ResetFileRunner({domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    path = tmp_path / "state/labs/LX001.json"
    state = load_state(path)
    argv = state["vms"]["node"]["virt_install_argv"]
    if record_format != "legacy":
        argv[10:14] = orchestrator._prebuilt_disk_args(overlay, overlay.with_name("seed.iso"))
        if record_format == "shorthand":
            argv[:] = [
                shlex.split(item)[0].removeprefix("xpath.set=").replace(",xpath.value=", "=", 1)
                if item.startswith("xpath.set=")
                else item
                for item in argv
            ]
    if tamper == "foreign-disk":
        argv[:] = [item.replace(str(overlay), "/foreign/disk.qcow2") for item in argv]
    elif tamper == "pool":
        argv[11] = "pool=node,vol=foreign"
    elif tamper == "extra-xml":
        argv.extend(["--xml", "./devices/disk[3]/source/@file=/foreign/disk.qcow2"])
    elif tamper == "uri":
        argv[2] = "qemu+ssh://foreign/system"
    else:
        argv[6] = "0"
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(OrchestrationError, match="invalid recreation record"):
        orchestrator.reset("LX001", images, readiness_probe=lambda *_: True)
    assert runner.calls == []
    assert overlay.read_bytes() == b"original overlay"


def test_reset_recreates_overlay_with_creation_time_virtual_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    domain, overlay, images, _vms = _single_reset_fixture(tmp_path)
    state_path = tmp_path / "state/labs/LX001.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["vms"]["node"]["address"] = "192.0.2.9"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    runner = ResetFileRunner({domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    events: list[tuple[str, Path | None]] = []
    real_run = runner.run
    real_fsync_file = orchestrator._fsync_file

    def record_run(argv: list[str], *, check: bool = True) -> CommandResult:
        if argv[0] == "virt-install":
            events.append(("virt-install", None))
        return real_run(argv, check=check)

    def record_fsync(path: Path) -> None:
        events.append(("fsync", path))
        real_fsync_file(path)

    monkeypatch.setattr(runner, "run", record_run)
    monkeypatch.setattr(orchestrator, "_fsync_file", record_fsync)

    reset = orchestrator.reset("LX001", images, readiness_probe=lambda *_: True)

    create = next(call for call in runner.calls if call[:2] == ("qemu-img", "create"))
    assert create[-1] == str(8 * 1024**3)
    assert reset["vms"]["node"]["address"] == "192.0.2.10"
    assert events.index(("fsync", overlay)) < events.index(("virt-install", None))


def test_reset_selected_vm_leaves_other_domain_disk_address_and_power_untouched(
    tmp_path: Path,
) -> None:
    target_domain, target_overlay, images, _vms = _single_reset_fixture(tmp_path)
    state_path = tmp_path / "state/labs/LX001.json"
    state = load_state(state_path)
    target = state["vms"]["node"]
    state["vm_order"] = ["controller", "node"]
    controller_domain = "labctl-u1000-lx001-controller"
    controller_dir = tmp_path / "data/instances/LX001/vms/controller"
    controller_dir.mkdir()
    controller_overlay = controller_dir / "disk.qcow2"
    controller_seed = controller_dir / "seed.iso"
    controller_overlay.write_bytes(b"learner controller disk")
    controller_seed.write_bytes(b"controller seed")
    controller = deepcopy(target)
    controller.update(
        {
            "domain": controller_domain,
            "address": "192.0.2.20",
            "overlay": str(controller_overlay),
            "seed": str(controller_seed),
            "virt_install_argv": [
                value.replace(target_domain, controller_domain)
                .replace(str(target_overlay), str(controller_overlay))
                .replace(str(target["seed"]), str(controller_seed))
                for value in target["virt_install_argv"]
            ],
        }
    )
    state["vms"] = {"controller": controller, "node": target}
    state["vms"]["node"]["address"] = "192.0.2.21"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    runner = ResetFileRunner({controller_domain: "running", target_domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    result = orchestrator.reset(
        "LX001", images, vm_names=("node",), readiness_probe=lambda *_: True
    )

    assert controller_overlay.read_bytes() == b"learner controller disk"
    assert result["vms"]["controller"]["address"] == "192.0.2.20"
    assert result["vms"]["node"]["address"] == "192.0.2.10"
    assert result["state"] == "ready"
    assert not any(controller_domain in call for call in runner.calls)


@pytest.mark.parametrize("selection", [(), ("missing",), ("node", "node"), ("../node",)])
def test_reset_rejects_malformed_vm_selection_before_provider_io(
    tmp_path: Path, selection: tuple[str, ...]
) -> None:
    domain, _overlay, images, _vms = _single_reset_fixture(tmp_path)
    runner = ResetFileRunner({domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError, match="reset VM selection"):
        orchestrator.reset("LX001", images, vm_names=selection)

    assert runner.calls == []


def test_network_ownership_is_parsed_not_matched_as_a_substring(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    _write_state(tmp_path, {"node": _vm(domain, tmp_path)})
    decoy = _ownership_xml("u1000-lx001", "network").replace("<", "&lt;").replace(">", "&gt;")
    runner = StateRunner(
        {domain: "shut off"}, network_xml=f"<network><description>{decoy}</description></network>"
    )
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError, match="ownership verification failed for network"):
        orchestrator.remove("LX001", force=True)

    assert not any(call[3] in {"net-destroy", "net-undefine"} for call in runner.calls)


def test_running_lab_remove_refusal_leaves_no_journal_or_state_changes(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    state_path = _write_state(tmp_path, {"node": _vm(domain, tmp_path, "ready")})
    original = state_path.read_bytes()
    runner = StateRunner({domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError, match="lab is running"):
        orchestrator.remove("LX001", force=False)

    assert state_path.read_bytes() == original
    assert not (tmp_path / "state/transactions/LX001.json").exists()
    stopped = orchestrator.stop("LX001", force=False)
    assert stopped["state"] == "stopped"


@pytest.mark.parametrize("pool_name", ["node", "controller", "labctl-u1000-lx001-node"])
def test_remove_never_adopts_or_deletes_unmarked_pools(tmp_path: Path, pool_name: str) -> None:
    domain = "labctl-u1000-lx001-node"
    _write_state(tmp_path, {"node": _vm(domain, tmp_path)})
    instance = tmp_path / "data/instances/LX001"
    instance.mkdir(parents=True)

    class ForeignPoolRunner(CleanupRunner):
        def __init__(self) -> None:
            super().__init__({domain})
            # A matching name and a target inside the instance are NOT ownership.
            self.pools = {pool_name: str(instance / "vms/node")}

        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            assert not argv[3].startswith("pool-"), "unmarked pools must be left untouched"
            return super().run(argv, check=check)

    runner = ForeignPoolRunner()
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    orchestrator.remove("LX001", force=True)
    assert runner.pools == {pool_name: str(instance / "vms/node")}
    assert not instance.exists()
    assert not (tmp_path / "state/labs/LX001.json").exists()


def test_lab_remove_retry_verifies_domain_already_recorded_missing(tmp_path: Path) -> None:
    missing = "labctl-u1000-lx001-missing"
    remaining = "labctl-u1000-lx001-remaining"
    _write_state(
        tmp_path,
        {
            "missing": _vm(missing, tmp_path, "missing"),
            "remaining": _vm(remaining, tmp_path),
        },
    )
    (tmp_path / "data/instances/LX001").mkdir(parents=True)
    runner = CleanupRunner({remaining})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    orchestrator.remove("LX001", force=True)

    assert not (tmp_path / "state/labs/LX001.json").exists()
    assert any(call[3] == "dominfo" and missing in call for call in runner.calls)
    assert not any(call[3] == "undefine" and missing in call for call in runner.calls)


def test_lab_remove_retry_skips_network_recorded_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = _write_state(tmp_path, {})
    instance = tmp_path / "data/instances/LX001"
    instance.mkdir(parents=True)
    runner = CleanupRunner(set())
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    real_rmtree = shutil.rmtree
    attempts = 0

    def fail_once(path: Path, *, ignore_errors: bool) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("local cleanup failed")
        real_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(shutil, "rmtree", fail_once)

    with pytest.raises(OrchestrationError, match="local cleanup failed"):
        orchestrator.remove("LX001", force=True)

    assert load_state(state_path)["cleanup"]["network"] == "missing"
    network_calls = len([call for call in runner.calls if call[3] == "net-dumpxml"])
    orchestrator.remove("LX001", force=True)
    assert len([call for call in runner.calls if call[3] == "net-dumpxml"]) == network_calls


def test_lab_remove_retry_skips_instance_recorded_missing_after_state_delete_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = _write_state(tmp_path, {})
    instance = tmp_path / "data/instances/LX001"
    instance.mkdir(parents=True)
    runner = CleanupRunner(set())
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    real_unlink = Path.unlink
    failed = False

    def fail_state_delete_once(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal failed
        if path == state_path and not failed:
            failed = True
            raise OSError("state deletion failed")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_state_delete_once)

    with pytest.raises(OrchestrationError, match="state deletion failed"):
        orchestrator.remove("LX001", force=True)

    assert load_state(state_path)["cleanup"]["instance"] == "missing"
    orchestrator.remove("LX001", force=True)
    assert not state_path.exists()


def test_vm_remove_retry_persists_domain_and_each_file_removal(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    vm_dir = tmp_path / "data/instances/LX001/vms/node"
    vm_dir.mkdir(parents=True)
    overlay = vm_dir / "disk.qcow2"
    seed = vm_dir / "seed.iso"
    overlay.mkdir()
    seed.mkdir()
    vm = _vm(domain, tmp_path)
    vm.update({"overlay": str(overlay), "seed": str(seed)})
    state_path = _write_state(tmp_path, {"node": vm})
    runner = CleanupRunner({domain})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError) as failure:
        orchestrator.remove_vm("LX001", "node", force=True)

    assert str(overlay) in str(failure.value)
    assert str(seed) in str(failure.value)
    failed_state = load_state(state_path)
    assert failed_state["vms"]["node"]["state"] == "missing"
    assert failed_state["cleanup"]["files"] == []

    overlay.rmdir()
    seed.rmdir()
    overlay.write_bytes(b"overlay")
    seed.write_bytes(b"seed")
    result = orchestrator.remove_vm("LX001", "node", force=True)

    assert result["cleanup"]["files"] == [str(overlay), str(seed)]
    assert not overlay.exists()
    assert not seed.exists()


def test_lab_remove_recovers_crash_after_domain_undefine(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    _write_state(tmp_path, {"node": _vm(domain, tmp_path)})
    (tmp_path / "data/instances/LX001").mkdir(parents=True)

    class CrashCleanupRunner(CleanupRunner):
        crashed = False

        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            result = super().run(argv, check=check)
            if argv[3] == "undefine" and not self.crashed:
                self.crashed = True
                raise KeyboardInterrupt
            return result

    runner = CrashCleanupRunner({domain})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(KeyboardInterrupt):
        orchestrator.remove("LX001", force=True)

    journal = tmp_path / "state/transactions/LX001.json"
    assert json.loads(journal.read_text(encoding="utf-8"))["operation"] == "remove"
    orchestrator.remove("LX001", force=True)

    assert not (tmp_path / "state/labs/LX001.json").exists()
    assert not journal.exists()
    assert len([call for call in runner.calls if call[3] == "undefine"]) == 1


def test_vm_remove_recovers_crash_after_owned_file_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    domain = "labctl-u1000-lx001-node"
    vm_dir = tmp_path / "data/instances/LX001/vms/node"
    vm_dir.mkdir(parents=True)
    overlay = vm_dir / "disk.qcow2"
    seed = vm_dir / "seed.iso"
    overlay.write_bytes(b"overlay")
    seed.write_bytes(b"seed")
    vm = _vm(domain, tmp_path, "missing")
    vm.update(overlay=str(overlay), seed=str(seed))
    _write_state(tmp_path, {"node": vm})
    orchestrator = KVMOrchestrator(
        tmp_path / "data", tmp_path / "state", runner=CleanupRunner(set())
    )
    real_unlink = os.unlink
    crashed = False

    def crash_after_unlink(path: str | bytes | Path, *args: object, **kwargs: object) -> None:
        nonlocal crashed
        real_unlink(path, *args, **kwargs)
        if path == overlay.name and kwargs.get("dir_fd") is not None and not crashed:
            crashed = True
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "unlink", crash_after_unlink)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.remove_vm("LX001", "node", force=True)

    journal = tmp_path / "state/transactions/LX001.json"
    assert json.loads(journal.read_text(encoding="utf-8"))["operation"] == "remove-vm"
    result = orchestrator.remove_vm("LX001", "node", force=True)

    assert result["cleanup"]["files"] == [str(overlay), str(seed)]
    assert not journal.exists()


def test_vm_remove_recovery_verifies_files_recorded_missing(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    vm_dir = tmp_path / "data/instances/LX001/vms/node"
    vm_dir.mkdir(parents=True)
    overlay = vm_dir / "disk.qcow2"
    seed = vm_dir / "seed.iso"
    overlay.write_bytes(b"overlay restored after crash")
    seed.write_bytes(b"seed restored after crash")
    vm = _vm(domain, tmp_path, "missing")
    vm.update(overlay=str(overlay), seed=str(seed))
    state_path = _write_state(tmp_path, {"node": vm})
    state = load_state(state_path)
    state["cleanup"] = {"files": [str(overlay), str(seed)]}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    journal = tmp_path / "state/transactions/LX001.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "LX001",
                "operation": "remove-vm",
                "provider_uri": "qemu:///system",
                "instance_uid": "u1000-lx001",
                "vm_name": "node",
                "domain": domain,
                "domain_stage": "missing",
                "files": {str(overlay): "missing", str(seed): "missing"},
            }
        ),
        encoding="utf-8",
    )
    orchestrator = KVMOrchestrator(
        tmp_path / "data", tmp_path / "state", runner=CleanupRunner(set())
    )

    orchestrator.remove_vm("LX001", "node", force=True)

    assert not overlay.exists()
    assert not seed.exists()
    assert not journal.exists()


def test_lab_remove_fsyncs_instance_parent_before_recording_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_state(tmp_path, {})
    instance = tmp_path / "data/instances/LX001"
    instance.mkdir(parents=True)
    (instance / "artifact").write_text("fixture", encoding="utf-8")
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        runner=CleanupRunner(set(), network_exists=False),
    )
    synced: set[Path] = set()
    real_fsync = os.fsync
    real_save_json = orchestrator_module.save_json

    def observe_fsync(descriptor: int) -> None:
        synced.add(Path(os.readlink(f"/proc/self/fd/{descriptor}")))
        real_fsync(descriptor)

    def require_parent_sync(path: Path, document: dict[str, object]) -> None:
        if document.get("instance_stage") == "missing":
            assert instance.parent in synced
        real_save_json(path, document)

    monkeypatch.setattr(os, "fsync", observe_fsync)
    monkeypatch.setattr(orchestrator_module, "save_json", require_parent_sync)

    orchestrator.remove("LX001", force=True)


def test_lab_remove_recovery_verifies_resources_recorded_missing(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    vm = _vm(domain, tmp_path, "missing")
    state_path = _write_state(tmp_path, {"node": vm})
    state = load_state(state_path)
    state["cleanup"] = {"network": "missing", "instance": "missing"}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    instance = tmp_path / "data/instances/LX001"
    instance.mkdir(parents=True)
    (instance / "artifact").write_text("restored after crash", encoding="utf-8")
    journal = tmp_path / "state/transactions/LX001.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "LX001",
                "operation": "remove",
                "provider_uri": "qemu:///system",
                "instance_uid": "u1000-lx001",
                "network": "labctl-u1000-lx001-network",
                "vm_order": ["node"],
                "domains": {"node": "missing"},
                "network_stage": "missing",
                "instance_stage": "missing",
            }
        ),
        encoding="utf-8",
    )
    runner = CleanupRunner({domain})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    orchestrator.remove("LX001", force=True)

    assert domain not in runner.domains
    assert not runner.network_exists
    assert not instance.exists()
    assert not state_path.exists()
    assert not journal.exists()


def test_stop_vm_stops_transitive_dependants_in_reverse_order(tmp_path: Path) -> None:
    domains = {name: f"labctl-u1000-lx001-{name}" for name in ("base", "middle", "leaf")}
    _write_state(
        tmp_path,
        {
            "base": _vm(domains["base"], tmp_path, "ready"),
            "middle": _vm(domains["middle"], tmp_path, "ready", ["base"]),
            "leaf": _vm(domains["leaf"], tmp_path, "ready", ["middle"]),
        },
    )
    runner = StateRunner({domain: "running" for domain in domains.values()})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    orchestrator.stop_vm("LX001", "base", force=True, wait_stopped=lambda *_: True)

    shutdowns = [call[4] for call in runner.calls if call[3] == "shutdown"]
    assert shutdowns == [domains["leaf"], domains["middle"], domains["base"]]


def test_remove_vm_refuses_present_transitive_dependants_before_io(tmp_path: Path) -> None:
    domains = {name: f"labctl-u1000-lx001-{name}" for name in ("base", "middle", "leaf")}
    _write_state(
        tmp_path,
        {
            "base": _vm(domains["base"], tmp_path),
            "middle": _vm(domains["middle"], tmp_path, "stopped", ["base"]),
            "leaf": _vm(domains["leaf"], tmp_path, "stopped", ["middle"]),
        },
    )
    runner = CleanupRunner(set(domains.values()))
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError, match=r"dependent VMs remain: middle, leaf"):
        orchestrator.remove_vm("LX001", "base", force=True)

    assert runner.calls == []


def test_foreign_domain_is_verified_before_stop_mutates_it(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    _write_state(tmp_path, {"node": _vm(domain, tmp_path, "running")})
    runner = StateRunner({domain: "running"})
    runner_metadata = runner.run

    def foreign_metadata(argv: list[str], *, check: bool = True) -> CommandResult:
        result = runner_metadata(argv, check=check)
        if "metadata" in argv:
            return CommandResult(result.argv, 0, _ownership_xml("someone-else", "domain"), "")
        return result

    runner.run = foreign_metadata  # type: ignore[method-assign]
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError, match="ownership verification failed"):
        orchestrator.stop("LX001", force=True)

    assert not any(call[3] in {"shutdown", "destroy"} for call in runner.calls)


def test_domain_ownership_requires_expected_vm_name(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    _write_state(tmp_path, {"node": _vm(domain, tmp_path, "running")})

    class LegacyMetadataRunner(StateRunner):
        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            if "metadata" in argv:
                return CommandResult(tuple(argv), 0, _ownership_xml("u1000-lx001", "domain"), "")
            return super().run(argv, check=check)

    runner = LegacyMetadataRunner({domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError, match="ownership verification failed"):
        orchestrator.stop("LX001", force=True)

    assert not any(call[3] in {"shutdown", "destroy"} for call in runner.calls)


def test_mutating_lifecycle_method_fails_nonblocking_on_lab_lock(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    _write_state(tmp_path, {"node": _vm(domain, tmp_path)})
    runner = StateRunner({domain: "shut off"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    lock = tmp_path / "state/locks/lab-LX001.lock"

    with file_lock(lock), pytest.raises(OrchestrationError, match="locked"):
        orchestrator.start("LX001", readiness_probe=lambda *_: True)

    assert runner.calls == []


def test_grade_session_holds_lifecycle_lock_until_context_exit(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    _write_state(tmp_path, {"node": _vm(domain, tmp_path)})
    runner = StateRunner({domain: "shut off"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    competing = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with orchestrator.grade_session("LX001"), pytest.raises(OrchestrationError, match="locked"):
        competing.start("LX001", readiness_probe=lambda *_: True)

    assert runner.calls == []


def test_start_observes_power_and_starts_transitive_dependencies(tmp_path: Path) -> None:
    domains = {name: f"labctl-u1000-lx001-{name}" for name in ("base", "middle", "leaf")}
    _write_state(
        tmp_path,
        {
            "base": _vm(domains["base"], tmp_path, "ready"),
            "middle": _vm(domains["middle"], tmp_path, depends_on=["base"]),
            "leaf": _vm(domains["leaf"], tmp_path, depends_on=["middle"]),
        },
    )
    runner = StateRunner({domain: "shut off" for domain in domains.values()})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    orchestrator.start("LX001", vm_name="leaf", readiness_probe=lambda *_: True)

    starts = [call[4] for call in runner.calls if call[3] == "start"]
    assert starts == [domains["base"], domains["middle"], domains["leaf"]]


def test_stop_observes_already_stopped_domain_without_mutating_it(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    _write_state(tmp_path, {"node": _vm(domain, tmp_path, "running")})
    runner = StateRunner({domain: "shut off"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    result = orchestrator.stop("LX001", force=False)

    assert result["vms"]["node"]["state"] == "stopped"
    assert not any(call[3] in {"shutdown", "destroy"} for call in runner.calls)


def test_start_failure_reports_original_and_restoration_failures(tmp_path: Path) -> None:
    first = "labctl-u1000-lx001-first"
    second = "labctl-u1000-lx001-second"
    _write_state(tmp_path, {"first": _vm(first, tmp_path), "second": _vm(second, tmp_path)})
    runner = StateRunner(
        {first: "shut off", second: "shut off"}, fail_start=second, fail_destroy=first
    )
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError) as error:
        orchestrator.start("LX001", readiness_probe=lambda *_: True)

    assert "failed start" in str(error.value)
    assert "restoration failures" in str(error.value)
    assert "failed restore" in str(error.value)


def test_destructive_lifecycle_rejects_traversal_identifiers_before_io(tmp_path: Path) -> None:
    runner = FakeRunner()
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError, match="invalid lab ID"):
        orchestrator.remove("../../victim", force=True)
    with pytest.raises(OrchestrationError, match="invalid VM name"):
        orchestrator.remove_vm("LX001", "../victim", force=True)

    assert runner.calls == []


def test_reset_rejects_tampered_virt_install_command_before_execution(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    vm = _vm(domain, tmp_path)
    vm.update(
        {
            "overlay": str(tmp_path / "data/instances/LX001/vms/node/disk.qcow2"),
            "seed": str(tmp_path / "data/instances/LX001/vms/node/seed.iso"),
            "image_digest": "a" * 64,
            "virt_install_argv": ["touch", str(tmp_path / "victim")],
        }
    )
    _write_state(tmp_path, {"node": vm})
    runner = StateRunner({domain: "shut off"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError, match="invalid recreation record"):
        orchestrator.reset("LX001", ImageStore(tmp_path / "images", runner=runner))

    assert not any(call[0] == "touch" for call in runner.calls)


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("uppercase", "lowercase SHA-256"),
        ("symlink", "non-symlink"),
        ("directory", "regular"),
        ("wrong-hash", "checksum"),
    ],
)
def test_reset_backing_blob_validation(tmp_path: Path, kind: str, message: str) -> None:
    images = ImageStore(tmp_path / "images")
    content = b"verified base"
    digest = hashlib.sha256(content).hexdigest()
    candidate = images.blobs / digest
    if kind == "uppercase":
        digest = digest.upper()
    elif kind == "symlink":
        target = tmp_path / "outside"
        target.write_bytes(content)
        candidate.symlink_to(target)
    elif kind == "directory":
        candidate.mkdir()
    else:
        candidate.write_bytes(b"tampered")
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state")

    with pytest.raises(OrchestrationError, match=message):
        orchestrator._validated_backing_blob(images, digest)


def test_vm_remove_refuses_tampered_paths_outside_instance(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    vm = _vm(domain, tmp_path, "missing")
    vm.update({"overlay": str(victim), "seed": str(victim)})
    _write_state(tmp_path, {"node": vm})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=FakeRunner())

    with pytest.raises(OrchestrationError, match="invalid overlay path"):
        orchestrator.remove_vm("LX001", "node", force=True)

    assert victim.read_text(encoding="utf-8") == "keep"


def test_vm_remove_refuses_symlinked_vm_directory(tmp_path: Path) -> None:
    domain = "labctl-u1000-lx001-node"
    vm_dir = tmp_path / "data/instances/LX001/vms/node"
    vm_dir.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    overlay = outside / "disk.qcow2"
    seed = outside / "seed.iso"
    overlay.write_bytes(b"keep overlay")
    seed.write_bytes(b"keep seed")
    vm_dir.symlink_to(outside, target_is_directory=True)
    vm = _vm(domain, tmp_path, "missing")
    vm.update(
        {
            "overlay": str(vm_dir / "disk.qcow2"),
            "seed": str(vm_dir / "seed.iso"),
        }
    )
    _write_state(tmp_path, {"node": vm})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=FakeRunner())

    with pytest.raises(OrchestrationError, match="symlink"):
        orchestrator.remove_vm("LX001", "node", force=True)

    assert overlay.read_bytes() == b"keep overlay"
    assert seed.read_bytes() == b"keep seed"


def test_create_retains_recoverable_state_when_libvirt_rollback_fails(tmp_path: Path) -> None:
    class RollbackRunner(FakeRunner):
        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            if "net-undefine" in argv:
                result = CommandResult(tuple(argv), 1, "", "busy")
                if check:
                    raise RuntimeError("network cleanup failed")
                self.calls.append(tuple(argv))
                return result
            return super().run(argv, check=check)

    runner = RollbackRunner(fail_on="virt-install")
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        runner=runner,
        keygen=lambda directory: _keys(directory),
        host_keygen=lambda directory: _host_keys(directory),
    )

    with pytest.raises(OrchestrationError, match="network cleanup failed"):
        orchestrator.create(_definition(tmp_path), "qemu:///session", images)

    failed = load_state(tmp_path / "state/labs/LX001.json")
    assert failed["state"] == "failed_cleanup"
    assert failed["cleanup_failures"]
    assert (tmp_path / "data/instances/LX001").is_dir()


def test_reset_failure_restores_each_domains_original_power(tmp_path: Path) -> None:
    uid = "u1000-lx001"
    network = "labctl-u1000-lx001-network"
    digest = hashlib.sha256(b"base").hexdigest()
    domains = {name: f"labctl-{uid}-{name}" for name in ("first", "second")}
    vms: dict[str, dict[str, object]] = {}
    for name, domain in domains.items():
        vm_dir = tmp_path / f"data/instances/LX001/vms/{name}"
        vm_dir.mkdir(parents=True)
        overlay = vm_dir / "disk.qcow2"
        seed = vm_dir / "seed.iso"
        overlay.write_bytes(b"overlay")
        seed.write_bytes(b"seed")
        vm = _vm(domain, tmp_path, "ready" if name == "first" else "stopped")
        vm.update(
            {
                "address": "192.0.2.1" if name == "first" else "192.0.2.2",
                "overlay": str(overlay),
                "seed": str(seed),
                "disk_size_bytes": 8 * 1024**3,
                "image_digest": digest,
                "virt_install_argv": [
                    "virt-install",
                    "--connect",
                    "qemu:///system",
                    "--name",
                    domain,
                    "--memory",
                    "1024",
                    "--vcpus",
                    "1",
                    "--import",
                    "--disk",
                    f"path={overlay},format=qcow2",
                    "--disk",
                    f"path={seed},device=cdrom",
                    "--network",
                    f"network={network},model=virtio",
                    "--osinfo",
                    "detect=on,require=off",
                    "--noautoconsole",
                    "--wait",
                    "0",
                ],
            }
        )
        vms[name] = vm
    _write_state(tmp_path, vms)
    images = ImageStore(tmp_path / "images")
    (images.blobs / digest).write_bytes(b"base")

    class ResetRunner(StateRunner):
        qemu_img_calls = 0

        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            if argv[0] == "qemu-img":
                self.qemu_img_calls += 1
                if self.qemu_img_calls == 2:
                    raise RuntimeError("overlay recreation failed")
                Path(argv[-2]).write_bytes(b"replacement overlay")
                return CommandResult(tuple(argv), 0, "", "")
            if argv[0] == "virt-install":
                self.calls.append(tuple(argv))
                self.power[argv[argv.index("--name") + 1]] = "running"
                return CommandResult(tuple(argv), 0, "", "")
            return super().run(argv, check=check)

    runner = ResetRunner({domains["first"]: "running", domains["second"]: "shut off"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError, match="overlay recreation failed"):
        orchestrator.reset("LX001", images)

    assert runner.power == {domains["first"]: "running", domains["second"]: "shut off"}
    failed = load_state(tmp_path / "state/labs/LX001.json")
    assert failed["vms"]["first"]["address"] == "192.0.2.1"
    assert failed["vms"]["second"]["address"] == "192.0.2.2"


def test_reset_persistence_failure_restores_original_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = "u1000-lx001"
    domain = f"labctl-{uid}-node"
    network = f"labctl-{uid}-network"
    vm_dir = tmp_path / "data/instances/LX001/vms/node"
    vm_dir.mkdir(parents=True)
    overlay = vm_dir / "disk.qcow2"
    seed = vm_dir / "seed.iso"
    overlay.write_bytes(b"original overlay")
    seed.write_bytes(b"seed")
    digest = hashlib.sha256(b"base").hexdigest()
    vm = _vm(domain, tmp_path, "ready")
    vm.update(
        {
            "overlay": str(overlay),
            "seed": str(seed),
            "disk_size_bytes": 8 * 1024**3,
            "image_digest": digest,
            "virt_install_argv": [
                "virt-install",
                "--connect",
                "qemu:///system",
                "--name",
                domain,
                "--memory",
                "1024",
                "--vcpus",
                "1",
                "--import",
                "--disk",
                f"path={overlay},format=qcow2",
                "--disk",
                f"path={seed},device=cdrom",
                "--network",
                f"network={network},model=virtio",
                "--osinfo",
                "detect=on,require=off",
                "--noautoconsole",
                "--wait",
                "0",
            ],
        }
    )
    _write_state(tmp_path, {"node": vm})
    images = ImageStore(tmp_path / "images")
    (images.blobs / digest).write_bytes(b"base")

    class ResetRunner(StateRunner):
        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            if argv[0] == "qemu-img":
                Path(argv[-2]).write_bytes(b"replacement overlay")
                return CommandResult(tuple(argv), 0, "", "")
            if argv[0] == "virt-install":
                self.calls.append(tuple(argv))
                self.power[argv[argv.index("--name") + 1]] = "running"
                return CommandResult(tuple(argv), 0, "", "")
            return super().run(argv, check=check)

    real_save_state = orchestrator_module.save_state
    failed = False

    def fail_final_save(path: Path, state: dict[str, object]) -> None:
        nonlocal failed
        if state.get("state") == "ready" and not failed:
            failed = True
            raise OSError("final state persistence failed")
        real_save_state(path, state)

    monkeypatch.setattr(orchestrator_module, "save_state", fail_final_save)
    runner = ResetRunner({domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError, match="final state persistence failed"):
        orchestrator.reset("LX001", images, readiness_probe=lambda *_: True)

    assert overlay.read_bytes() == b"original overlay"
    assert not overlay.with_suffix(".qcow2.reset-backup").exists()


def test_reset_crash_after_backup_is_recovered_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    domain, overlay, images, _vms = _single_reset_fixture(tmp_path)
    runner = ResetFileRunner({domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    real_replace = Path.replace
    crashed = False

    def crash_after_backup(source: Path, target: Path) -> Path:
        nonlocal crashed
        result = real_replace(source, target)
        if target.name.endswith(".reset-backup") and not crashed:
            crashed = True
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(Path, "replace", crash_after_backup)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.reset("LX001", images, readiness_probe=lambda *_: True)

    journal = tmp_path / "state/transactions/LX001.json"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "rollback"

    result = orchestrator.reset("LX001", images, readiness_probe=lambda *_: True)

    assert result["state"] == "ready"
    assert overlay.read_bytes() == b"replacement overlay"
    assert not journal.exists()


def test_normal_reset_after_selective_reset_crash_preserves_journal_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_domain, target_overlay, images, target_vms = _single_reset_fixture(tmp_path)
    controller_domain = "labctl-u1000-lx001-controller"
    controller_dir = tmp_path / "data/instances/LX001/vms/controller"
    controller_dir.mkdir()
    controller_overlay = controller_dir / "disk.qcow2"
    controller_seed = controller_dir / "seed.iso"
    controller_overlay.write_bytes(b"controller disk containing learner site.yml")
    controller_seed.write_bytes(b"controller seed")
    controller = deepcopy(target_vms["node"])
    target_seed = Path(str(target_vms["node"]["seed"]))
    recreation = target_vms["node"]["virt_install_argv"]
    assert isinstance(recreation, list)
    controller.update(
        {
            "domain": controller_domain,
            "overlay": str(controller_overlay),
            "seed": str(controller_seed),
            "virt_install_argv": [
                value.replace(target_domain, controller_domain)
                .replace(str(target_overlay), str(controller_overlay))
                .replace(str(target_seed), str(controller_seed))
                if isinstance(value, str)
                else value
                for value in recreation
            ],
        }
    )
    _write_state(tmp_path, {"node": target_vms["node"], "controller": controller})
    runner = ResetFileRunner({target_domain: "running", controller_domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    real_replace = Path.replace
    crashed = False

    def crash_after_target_backup(source: Path, target: Path) -> Path:
        nonlocal crashed
        result = real_replace(source, target)
        if source == target_overlay and target.name.endswith(".reset-backup") and not crashed:
            crashed = True
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(Path, "replace", crash_after_target_backup)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.reset("LX001", images, vm_names=("node",), readiness_probe=lambda *_: True)

    result = orchestrator.reset("LX001", images, readiness_probe=lambda *_: True)

    assert result["state"] == "ready"
    assert target_overlay.read_bytes() == b"replacement overlay"
    assert controller_overlay.read_bytes() == b"controller disk containing learner site.yml"
    assert not any(
        controller_domain in call
        for call in runner.calls
        if call[0] == "virt-install" or "undefine" in call
    )


def test_grade_session_recovers_interrupted_reset_before_fresh_selective_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    domain, overlay, images, _vms = _single_reset_fixture(tmp_path)
    runner = ResetFileRunner({domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    real_replace = Path.replace
    crashed = False

    def crash_after_backup(source: Path, target: Path) -> Path:
        nonlocal crashed
        result = real_replace(source, target)
        if target.name.endswith(".reset-backup") and not crashed:
            crashed = True
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(Path, "replace", crash_after_backup)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.reset("LX001", images, vm_names=("node",), readiness_probe=lambda *_: True)

    with orchestrator.grade_session("LX001") as session:
        result = session.reset(images, vm_names=("node",))

    assert result["state"] == "ready"
    assert overlay.read_bytes() == b"replacement overlay"
    assert not (tmp_path / "state/transactions/LX001.json").exists()


def test_start_refreshes_address_without_replacing_stopped_vm_disk(tmp_path: Path) -> None:
    domain, overlay, _images, _vms = _single_reset_fixture(tmp_path)
    state_path = tmp_path / "state/labs/LX001.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["state"] = "stopped"
    state["vms"]["node"]["state"] = "stopped"
    state["vms"]["node"]["address"] = "192.0.2.10"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    class ChangedAddressRunner(StateRunner):
        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            if "domifaddr" in argv:
                return CommandResult(
                    tuple(argv),
                    0,
                    " vnet0  52:54:00:00:00:01  ipv4  192.0.2.77/24\n",
                    "",
                )
            return super().run(argv, check=check)

    runner = ChangedAddressRunner({domain: "shut off"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    result = orchestrator.start(
        "LX001", readiness_probe=lambda _user, address: address == "192.0.2.77"
    )

    assert result["vms"]["node"]["address"] == "192.0.2.77"
    assert overlay.read_bytes() == b"original overlay"
    assert not any(call[0] in {"qemu-img", "virt-install"} for call in runner.calls)


def test_reset_backup_cleanup_failure_keeps_committed_replacement_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    domain, overlay, images, _vms = _single_reset_fixture(tmp_path)
    runner = ResetFileRunner({domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    real_unlink = Path.unlink
    failed = False

    def retain_backup(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal failed
        if path.name.endswith(".reset-backup") and not failed:
            failed = True
            raise OSError("backup cleanup failed")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", retain_backup)
    result = orchestrator.reset("LX001", images, readiness_probe=lambda *_: True)

    backup = overlay.with_suffix(".qcow2.reset-backup")
    assert result["state"] == "ready"
    assert result["cleanup"]["reset_backups"] == [str(backup)]
    assert overlay.read_bytes() == b"replacement overlay"
    assert backup.read_bytes() == b"original overlay"
    creates = len([call for call in runner.calls if call[0] == "qemu-img"])

    retried = orchestrator.reset("LX001", images, readiness_probe=lambda *_: True)

    assert retried["state"] == "ready"
    assert "reset_backups" not in retried.get("cleanup", {})
    assert overlay.read_bytes() == b"replacement overlay"
    assert not backup.exists()
    assert len([call for call in runner.calls if call[0] == "qemu-img"]) == creates


@pytest.mark.parametrize("drifted", [False, True])
def test_reset_recovers_unowned_planned_domain_only_when_material_matches(
    tmp_path: Path, drifted: bool
) -> None:
    domain, overlay, images, vms = _single_reset_fixture(tmp_path)
    vm = vms["node"]
    network = "labctl-u1000-lx001-network"

    class MetadataCrashRunner(ResetFileRunner):
        partial = False
        crashed = False

        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            command = argv[3] if argv[0] == "virsh" else argv[0]
            if argv[0] == "virt-install":
                result = super().run(argv, check=check)
                self.partial = True
                return result
            if command == "metadata" and "--set" in argv and self.partial:
                if not self.crashed:
                    self.crashed = True
                    raise KeyboardInterrupt
                self.partial = False
            if command == "metadata" and "--set" not in argv and self.partial:
                return CommandResult(tuple(argv), 1, "", "metadata missing")
            if command == "dumpxml":
                memory = "2048" if drifted else "1024"
                xml = (
                    f"<domain><name>{domain}</name><memory unit='MiB'>{memory}</memory>"
                    f"<vcpu>1</vcpu><devices><disk device='disk'><source file='{overlay}'/>"
                    f"</disk><disk device='cdrom'><source file='{vm['seed']}'/></disk>"
                    f"<interface type='network'><source network='{network}'/>"
                    "<model type='virtio'/></interface></devices></domain>"
                )
                return CommandResult(tuple(argv), 0, xml, "")
            return super().run(argv, check=check)

    runner = MetadataCrashRunner({domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.reset("LX001", images, readiness_probe=lambda *_: True)

    journal_path = tmp_path / "state/transactions/LX001.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["vms"]["node"]["stage"] == "virt-install-planned"
    assert journal["vms"]["node"]["material"][0] == domain

    if drifted:
        with pytest.raises(OrchestrationError, match="material"):
            orchestrator.reset("LX001", images, readiness_probe=lambda *_: True)
        assert overlay.read_bytes() == b"replacement overlay"
        assert overlay.with_suffix(".qcow2.reset-backup").exists()
    else:
        result = orchestrator.reset("LX001", images, readiness_probe=lambda *_: True)
        assert result["state"] == "ready"
        assert not journal_path.exists()


def _reset_journal(tmp_path: Path) -> tuple[dict[str, object], Path]:
    _domain, overlay, _images, _vms = _single_reset_fixture(tmp_path)
    state = load_state(tmp_path / "state/labs/LX001.json")
    return (
        {
            "schema_version": 1,
            "id": "LX001",
            "operation": "reset",
            "phase": "rollback",
            "state": state,
            "before": {"node": True},
            "vms": {
                "node": {
                    "overlay": str(overlay),
                    "backup": str(overlay.with_suffix(".qcow2.reset-backup")),
                    "recreation": state["vms"]["node"]["virt_install_argv"],
                    "stage": "backup-planned",
                    "material": None,
                }
            },
        },
        overlay,
    )


def test_selective_reset_recovery_rolls_back_only_journaled_vm(tmp_path: Path) -> None:
    journal, overlay = _reset_journal(tmp_path)
    state = journal["state"]
    assert isinstance(state, dict)
    controller_domain = "labctl-u1000-lx001-controller"
    state["vm_order"] = ["controller", "node"]
    vms = state["vms"]
    assert isinstance(vms, dict)
    vms["controller"] = _vm(controller_domain, tmp_path, "ready")
    journal["selected_vms"] = ["node"]
    backup = overlay.with_suffix(".qcow2.reset-backup")
    overlay.replace(backup)
    overlay.write_bytes(b"replacement overlay")

    class RecoveryRunner(ResetFileRunner):
        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            if "undefine" in argv:
                self.power.pop(argv[4], None)
            if "dominfo" in argv and argv[4] not in self.power:
                self.calls.append(tuple(argv))
                return CommandResult(tuple(argv), 1, "", f"failed to get domain '{argv[4]}'")
            return super().run(argv, check=check)

    runner = RecoveryRunner({controller_domain: "running", "labctl-u1000-lx001-node": "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    recovered, committed = orchestrator._recover_reset(journal)

    assert committed is False
    assert overlay.read_bytes() == b"original overlay"
    assert recovered["vms"]["controller"]["state"] == "ready"
    assert not any(controller_domain in call for call in runner.calls)
    installs = [call for call in runner.calls if call[0] == "virt-install"]
    assert installs
    for argv in installs:
        assert [argv[i + 1] for i, arg in enumerate(argv) if arg == "--disk"] == ["none"]
    assert not any(arg.startswith("pool-") for call in runner.calls for arg in call)


@pytest.mark.parametrize(
    "tamper",
    ["phase", "vm-name", "overlay", "backup-name", "state-id", "recreation"],
)
def test_reset_recovery_validates_complete_journal_before_io(tmp_path: Path, tamper: str) -> None:
    journal, overlay = _reset_journal(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    candidate = deepcopy(journal)
    records = candidate["vms"]
    assert isinstance(records, dict)
    record = records["node"]
    assert isinstance(record, dict)
    if tamper == "phase":
        candidate["phase"] = "unknown"
    elif tamper == "vm-name":
        candidate["vms"] = {"../node": record}
    elif tamper == "overlay":
        record["overlay"] = str(victim)
    elif tamper == "backup-name":
        record["backup"] = str(victim)
    elif tamper == "state-id":
        state = candidate["state"]
        assert isinstance(state, dict)
        state["id"] = "OTHER"
    else:
        record["recreation"] = ["touch", str(victim)]
    runner = FakeRunner()
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError, match="invalid reset transaction"):
        orchestrator._recover_reset(candidate)

    assert victim.read_text(encoding="utf-8") == "keep"
    assert overlay.read_bytes() == b"original overlay"
    assert runner.calls == []


@pytest.mark.parametrize("field", ["overlay", "seed"])
def test_reset_rejects_non_regular_vm_files_before_mutation(tmp_path: Path, field: str) -> None:
    domain, _overlay, images, vms = _single_reset_fixture(tmp_path)
    path = Path(str(vms["node"][field]))
    path.unlink()
    path.mkdir()
    runner = StateRunner({domain: "running"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError, match=f"invalid {field} path"):
        orchestrator.reset("LX001", images)

    assert runner.calls == []


@pytest.mark.parametrize("vm_name", ["../node", "node; touch victim", None])
def test_recovery_error_never_suggests_invalid_vm_command(tmp_path: Path, vm_name: object) -> None:
    runner = FakeRunner()
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    journal = tmp_path / "state/transactions/LX001.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps(
            {"schema_version": 1, "id": "LX001", "operation": "remove-vm", "vm_name": vm_name}
        )
    )
    with pytest.raises(OrchestrationError, match="inspect the transaction journal") as caught:
        orchestrator.remove("LX001", force=True)
    assert "labctl vm rm" not in str(caught.value)
    assert runner.calls == []


def test_remove_unbound_create_journal_performs_no_provider_io(tmp_path: Path) -> None:
    runner = FakeRunner()
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    uid = "550e8400-e29b-41d4-a716-446655440000"
    journal = tmp_path / "state/transactions/LX001.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "LX001",
                "operation": "create",
                "provider_uri": "qemu:///system",
                "instance_uid": uid,
                "instance_owned": True,
                "network": f"labctl-{uid}-network",
                "vm_order": [],
                "vms": {},
            }
        )
    )
    orchestrator.remove("LX001", force=False)
    assert not journal.exists()
    assert runner.calls == []


@pytest.mark.parametrize("tamper", ["identity", "material"])
def test_remove_rejects_tampered_create_journal_before_provider_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    orchestrator, runner, _images, journal, state = _pending_create(tmp_path, monkeypatch)
    transaction = json.loads(journal.read_text())
    if tamper == "identity":
        transaction["instance_uid"] = "foreign"
    else:
        transaction["vms"]["node"]["material"][0] = "foreign"
    journal.write_text(json.dumps(transaction))
    before = journal.read_bytes()
    runner.calls.clear()
    with pytest.raises(OrchestrationError, match="invalid create transaction"):
        orchestrator.remove("LX001", force=True)
    assert journal.read_bytes() == before
    assert load_state(tmp_path / "state/labs/LX001.json") == state
    assert (tmp_path / "data/instances/LX001").exists()
    assert runner.calls == []


def test_grade_reset_backup_recovery_error_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=FakeRunner())
    journal = tmp_path / "state/transactions/LX001.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"schema_version": 1, "id": "LX001", "operation": "reset"}))
    monkeypatch.setattr(orchestrator, "_recover_reset", lambda _: ({}, True))
    with (
        pytest.raises(
            OrchestrationError, match=r"pending transaction \(reset\).*labctl lab reset LX001"
        ),
        orchestrator.grade_session("LX001"),
    ):
        pytest.fail("backup cleanup must block grading")


@pytest.mark.parametrize(
    ("pending", "command", "operation"),
    [
        (pending, command, operation)
        for pending, command in [
            ("create", "labctl lab rm LX001"),
            ("reset", "labctl lab reset LX001"),
            ("remove", "labctl lab rm LX001"),
            ("remove-vm", "labctl vm rm LX001 other --force"),
            ("unknown", "inspect the transaction journal"),
        ]
        for operation in [
            "start",
            "stop",
            "restart",
            "stop_vm",
            "remove_vm",
            "remove",
            "reconcile",
            "reset",
            "create",
            "grade",
        ]
        # Matching recovery paths are exercised separately, not rejection cases.
        if (pending, operation)
        not in {
            ("create", "create"),
            ("create", "remove"),
            ("reset", "reset"),
            ("reset", "grade"),
            ("remove", "remove"),
        }
    ],
)
def test_recovery_error_identifies_pending_operation(
    tmp_path: Path, pending: str, command: str, operation: str
) -> None:
    runner = FakeRunner()
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    definition = _definition(tmp_path)
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)
    journal = tmp_path / "state/transactions/LX001.json"
    journal.parent.mkdir(parents=True)
    content = json.dumps(
        {"schema_version": 1, "id": "LX001", "operation": pending, "vm_name": "other"}
    )
    journal.write_text(content)
    with pytest.raises(OrchestrationError) as caught:
        if operation == "create":
            orchestrator.create(definition, "qemu:///system", images)
        elif operation == "reset":
            orchestrator.reset("LX001", images)
        elif operation == "grade":
            with orchestrator.grade_session("LX001"):
                pytest.fail("pending transaction must block grading")
        elif operation == "start":
            orchestrator.start("LX001")
        elif operation in {"stop", "restart", "remove"}:
            getattr(orchestrator, operation)("LX001", force=True)
        elif operation in {"stop_vm", "remove_vm"}:
            getattr(orchestrator, operation)("LX001", "node", force=True)
        else:
            orchestrator.reconcile("LX001", repair=True)
    assert f"pending transaction ({pending})" in str(caught.value)
    assert command in str(caught.value)
    if pending == "create":
        assert "labctl lab create LX001" in str(caught.value)
        assert "lab reset" not in str(caught.value)
    assert journal.read_text() == content
    assert runner.calls == []


@pytest.mark.parametrize(
    "operation", ["start", "stop", "restart", "stop_vm", "remove_vm", "remove", "reconcile"]
)
def test_pending_transaction_blocks_other_lifecycle_operations(
    tmp_path: Path, operation: str
) -> None:
    domain = "labctl-u1000-lx001-node"
    _write_state(tmp_path, {"node": _vm(domain, tmp_path, "stopped")})
    journal = tmp_path / "state/transactions/LX001.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "LX001",
                "operation": "reset",
                "phase": "recovery-required",
            }
        ),
        encoding="utf-8",
    )
    runner = StateRunner({domain: "shut off"})
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    with pytest.raises(OrchestrationError, match=r"pending transaction.*lab reset"):
        if operation == "start":
            orchestrator.start("LX001")
        elif operation == "stop":
            orchestrator.stop("LX001", force=True)
        elif operation == "restart":
            orchestrator.restart("LX001", force=True)
        elif operation == "stop_vm":
            orchestrator.stop_vm("LX001", "node", force=True)
        elif operation == "remove_vm":
            orchestrator.remove_vm("LX001", "node", force=True)
        elif operation == "remove":
            orchestrator.remove("LX001", force=True)
        else:
            orchestrator.reconcile("LX001", repair=True)

    assert runner.calls == []


def test_reconcile_repairs_missing_network_into_retryable_cleanup_state(tmp_path: Path) -> None:
    state_path = _write_state(tmp_path, {})
    runner = CleanupRunner(set(), network_exists=False)
    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=runner)

    actions = orchestrator.reconcile("LX001", repair=True)

    assert actions == [
        {
            "resource": "labctl-u1000-lx001-network",
            "action": "mark-missing",
            "reason": "network absent",
        }
    ]
    repaired = load_state(state_path)
    assert repaired["cleanup"]["network"] == "missing"
    assert repaired["state"] == "failed_cleanup"


def test_domain_probe_error_is_not_treated_as_missing_during_rollback(tmp_path: Path) -> None:
    class ProbeErrorRunner(FakeRunner):
        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            if "dominfo" in argv:
                return CommandResult(tuple(argv), 1, "", "permission denied")
            return super().run(argv, check=check)

    orchestrator = KVMOrchestrator(tmp_path / "data", tmp_path / "state", runner=ProbeErrorRunner())

    with pytest.raises(OrchestrationError, match="permission denied"):
        orchestrator._cleanup_created_domain(
            "qemu:///system", "labctl-u1000-lx001-node", "u1000-lx001", "node"
        )


def test_network_probe_error_is_not_persisted_as_missing_during_reconcile(tmp_path: Path) -> None:
    state_path = _write_state(tmp_path, {})

    class ProbeErrorRunner(CleanupRunner):
        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            if "net-info" in argv:
                return CommandResult(tuple(argv), 1, "", "failed to connect to hypervisor")
            return super().run(argv, check=check)

    orchestrator = KVMOrchestrator(
        tmp_path / "data", tmp_path / "state", runner=ProbeErrorRunner(set())
    )

    with pytest.raises(OrchestrationError, match="failed to connect to hypervisor"):
        orchestrator.reconcile("LX001", repair=True)

    assert "cleanup" not in load_state(state_path)


@pytest.mark.parametrize(
    ("drift", "reason"),
    [
        ("network", "network configuration drift"),
        ("domain", "domain configuration drift"),
        ("attachment", "domain configuration drift"),
        ("backing", "disk backing drift"),
        ("power", "power state drift"),
    ],
)
def test_reconcile_reports_material_grading_drift(tmp_path: Path, drift: str, reason: str) -> None:
    uid = "u1000-lx001"
    network = f"labctl-{uid}-network"
    domain = f"labctl-{uid}-node"
    vm_dir = tmp_path / "data/instances/LX001/vms/node"
    vm_dir.mkdir(parents=True)
    overlay = vm_dir / "disk.qcow2"
    seed = vm_dir / "seed.iso"
    backing = tmp_path / "images/blobs" / ("a" * 64)
    backing.parent.mkdir(parents=True)
    for file in (overlay, seed, backing):
        file.write_bytes(b"fixture")
    vm = _vm(domain, tmp_path, "ready")
    vm.update(
        {
            "overlay": str(overlay),
            "seed": str(seed),
            "backing_file": str(backing),
            "image_digest": "a" * 64,
            "virt_install_argv": [
                "virt-install",
                "--connect",
                "qemu:///system",
                "--name",
                domain,
                "--memory",
                "1024",
                "--vcpus",
                "1",
                "--import",
                "--disk",
                f"path={overlay},format=qcow2",
                "--disk",
                f"path={seed},device=cdrom",
                "--network",
                f"network={network},model=virtio",
                "--osinfo",
                "detect=on,require=off",
                "--noautoconsole",
                "--wait",
                "0",
            ],
        }
    )
    _write_state(tmp_path, {"node": vm})

    class DriftRunner(StateRunner):
        def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
            call = tuple(argv)
            if argv[0] == "qemu-img":
                if drift != "power":
                    raise AssertionError("active-domain reconciliation must use the QEMU monitor")
                return CommandResult(call, 0, json.dumps({"backing-filename": str(backing)}), "")
            command = argv[3]
            if command == "qemu-monitor-command":
                observed = backing if drift != "backing" else tmp_path / "foreign"
                output = {
                    "return": [
                        {
                            "inserted": {
                                "file": str(overlay),
                                "backing_file": str(observed),
                            }
                        }
                    ]
                }
                return CommandResult(call, 0, json.dumps(output), "")
            if command == "net-dumpxml" and "metadata" not in argv:
                xml = KVMOrchestrator(tmp_path / "data", tmp_path / "state")._network_xml(
                    network, uid
                )
                if drift == "network":
                    xml = xml.replace('mode="nat"', 'mode="route"')
                return CommandResult(call, 0, xml, "")
            if command == "dumpxml":
                memory = "2048" if drift == "domain" else "1024"
                disk = tmp_path / "foreign" if drift == "attachment" else overlay
                xml = (
                    f"<domain><name>{domain}</name>"
                    f"<memory unit='MiB'>{memory}</memory><vcpu>1</vcpu>"
                    f"<devices><disk device='disk'><source file='{disk}'/></disk>"
                    f"<disk device='cdrom'><source file='{seed}'/></disk>"
                    f"<interface type='network'><source network='{network}'/>"
                    "<model type='virtio'/></interface></devices></domain>"
                )
                return CommandResult(call, 0, xml, "")
            if command == "domstate":
                power = "shut off" if drift == "power" else "running"
                return CommandResult(call, 0, power + "\n", "")
            return super().run(argv, check=check)

    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        runner=DriftRunner({domain: "running"}),
    )

    actions = orchestrator.reconcile("LX001", repair=False)

    assert any(reason in action["reason"] for action in actions)
