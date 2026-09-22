from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from labctl.application import Application
from labctl.cli import parse_args
from labctl.config import Config
from labctl.definitions import GradingDefinition, load_definition
from labctl.errors import ExitStatus, LabctlError
from labctl.grading import GradeOutcome, GradeResult
from labctl.images import ImageStore
from labctl.kvm import KVMProvider
from labctl.provider import ProviderHealth


class DriftedOrchestrator:
    @contextmanager
    def grade_session(self, lab_id: str):  # type: ignore[no-untyped-def]
        assert lab_id == "LX001"
        yield self

    def reconcile(self, *, repair: bool) -> list[dict[str, str]]:
        assert repair is False
        return [
            {
                "resource": "labctl-u1000-lx001-node",
                "action": "manual-recovery",
                "reason": "foreign or altered ownership",
            }
        ]


def test_lab_list_includes_title_immediately_after_id(tmp_path: Path) -> None:
    app = Application(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        runtime_root=tmp_path / "runtime",
    )

    result = app.execute(parse_args(["lab", "list"]))

    assert result.columns == ("id", "title", "state", "provider", "uri")
    titles = {row["id"]: row["title"] for row in result.data}
    assert titles["LX001"] == "Users and permissions"
    assert titles["AN001"] == "Ansible controller and web target"


def test_missing_lab_lifecycle_preserves_not_found_status(tmp_path: Path) -> None:
    app = Application(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )

    with pytest.raises(LabctlError) as caught:
        app.execute(parse_args(["lab", "start", "LX999"]))

    assert caught.value.status == ExitStatus.NOT_FOUND
    assert str(caught.value) == "lab not found: LX999"


def test_missing_vm_lifecycle_preserves_not_found_status(tmp_path: Path) -> None:
    state = tmp_path / "state/labs/LX001.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "LX001",
                "provider": "kvm",
                "provider_uri": "qemu:///system",
                "instance_uid": "u1000-lx001",
                "network": "labctl-u1000-lx001-network",
                "state": "stopped",
                "vm_order": [],
                "vms": {},
            }
        ),
        encoding="utf-8",
    )
    app = Application(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )

    with pytest.raises(LabctlError) as caught:
        app.execute(parse_args(["vm", "start", "LX001", "missing"]))

    assert caught.value.status == ExitStatus.NOT_FOUND
    assert str(caught.value) == "VM not found: missing"


def test_grade_refuses_resource_drift_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state/labs/LX001.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "LX001",
                "provider_uri": "qemu:///system",
                "state": "ready",
                "vms": {},
            }
        ),
        encoding="utf-8",
    )
    app = Application(data_root=tmp_path / "data", state_root=tmp_path / "state")
    monkeypatch.setattr(Application, "_orchestrator", lambda self, config: DriftedOrchestrator())

    with pytest.raises(LabctlError, match=r"grading refused.*drift"):
        app._lab_grade(SimpleNamespace(lab="LX001"), Config())


@pytest.mark.parametrize("reset_vms", [("target",), ()])
def test_grade_resets_only_opted_in_vms_and_uses_refreshed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_vms: tuple[str, ...]
) -> None:
    lab_id = "AN001" if reset_vms else "LX001"
    state_path = tmp_path / f"state/labs/{lab_id}.json"
    state_path.parent.mkdir(parents=True)

    def state(address: str) -> dict[str, object]:
        hosts = {}
        for name in ("controller", "target"):
            hosts[name] = {
                "hostname": f"{name}.lab",
                "address": address if name == "target" else "192.0.2.2",
                "ssh_user": "student",
                "identity_file": str(tmp_path / "id_lab"),
                "known_hosts": str(tmp_path / "known_hosts"),
                "state": "ready",
            }
        return {
            "schema_version": 1,
            "id": lab_id,
            "provider": "kvm",
            "provider_uri": "qemu:///system",
            "state": "ready",
            "vms": hosts,
        }

    state_path.write_text(json.dumps(state("192.0.2.10")), encoding="utf-8")
    snapshot = tmp_path / f"data/instances/{lab_id}/definition/lab.yaml"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("fixture", encoding="utf-8")
    calls: list[tuple[str, tuple[str, ...]]] = []

    class Orchestrator:
        @contextmanager
        def grade_session(self, selected: str):  # type: ignore[no-untyped-def]
            assert selected == lab_id
            yield self

        def reconcile(self, *, repair: bool):  # type: ignore[no-untyped-def]
            return []

        def reset(self, images: ImageStore, *, vm_names: tuple[str, ...]):
            calls.append((lab_id, vm_names))
            return state("192.0.2.99")

        def start(self):  # type: ignore[no-untyped-def]
            return state("192.0.2.99")

    definition = SimpleNamespace(
        title="Test", goal="Test", grader=tmp_path / "grade", grading=GradingDefinition(reset_vms)
    )
    observed: dict[str, object] = {}
    monkeypatch.setattr(Application, "_orchestrator", lambda self, config: Orchestrator())
    monkeypatch.setattr("labctl.application.load_definition", lambda source: definition)

    def grade(argv, context, *, timeout):  # type: ignore[no-untyped-def]
        observed.update(context)
        return GradeResult(GradeOutcome.PASS, 0, (), ())

    monkeypatch.setattr("labctl.application.run_grader", grade)
    app = Application(data_root=tmp_path / "data", state_root=tmp_path / "state")

    app._lab_grade(SimpleNamespace(lab=lab_id), Config())

    assert calls == ([(lab_id, reset_vms)] if reset_vms else [])
    assert observed["hosts"]["target"]["address"] == ("192.0.2.99" if reset_vms else "192.0.2.10")


def test_grade_holds_one_lifecycle_session_through_grader_and_starts_preserved_vm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab_id = "AN001"
    snapshot = tmp_path / f"data/instances/{lab_id}/definition/lab.yaml"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("fixture", encoding="utf-8")
    events: list[str] = []

    def state(controller_address: str, target_address: str) -> dict[str, object]:
        return {
            "provider": "kvm",
            "provider_uri": "qemu:///system",
            "vms": {
                name: {
                    "hostname": f"{name}.lab",
                    "address": address,
                    "ssh_user": "student",
                    "identity_file": str(tmp_path / "id_lab"),
                    "known_hosts": str(tmp_path / "known_hosts"),
                    "state": "ready",
                }
                for name, address in (
                    ("controller", controller_address),
                    ("target", target_address),
                )
            },
        }

    class Session:
        def reconcile(self, *, repair: bool):  # type: ignore[no-untyped-def]
            events.append("reconcile")
            return []

        def reset(self, images: ImageStore, *, vm_names: tuple[str, ...]):
            events.append(f"reset:{','.join(vm_names)}")
            return state("192.0.2.2", "192.0.2.99")

        def start(self):  # type: ignore[no-untyped-def]
            events.append("start")
            return state("192.0.2.88", "192.0.2.99")

    class Orchestrator:
        @contextmanager
        def grade_session(self, selected: str):  # type: ignore[no-untyped-def]
            assert selected == lab_id
            events.append("lock-enter")
            try:
                yield Session()
            finally:
                events.append("lock-exit")

    definition = SimpleNamespace(
        title="Test",
        goal="Test",
        grader=tmp_path / "grade",
        grading=GradingDefinition(("target",)),
    )
    observed: dict[str, object] = {}
    monkeypatch.setattr(Application, "_orchestrator", lambda self, config: Orchestrator())
    monkeypatch.setattr("labctl.application.load_definition", lambda source: definition)

    def grade(argv, context, *, timeout):  # type: ignore[no-untyped-def]
        events.append("grader")
        observed.update(context)
        return GradeResult(GradeOutcome.PASS, 0, (), ())

    monkeypatch.setattr("labctl.application.run_grader", grade)
    app = Application(data_root=tmp_path / "data", state_root=tmp_path / "state")

    app._lab_grade(SimpleNamespace(lab=lab_id), Config())

    assert events == [
        "lock-enter",
        "reconcile",
        "reset:target",
        "start",
        "grader",
        "lock-exit",
    ]
    hosts = observed["hosts"]
    assert isinstance(hosts, dict)
    assert hosts["controller"]["address"] == "192.0.2.88"


def _external_definition(tmp_path: Path, provider: str = "other"):  # type: ignore[no-untyped-def]
    root = tmp_path / "external"
    root.mkdir()
    for name in ("setup.sh", "grade.sh"):
        script = root / name
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o700)
    (root / "lab.yaml").write_text(
        f"""schema_version: 1
id: LX001
title: External
goal: Test
instructions: Test
provider: {provider}
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
""",
        encoding="utf-8",
    )
    return load_definition(root / "lab.yaml", external=True)


def test_create_fails_before_resources_for_unsupported_non_kvm_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = _external_definition(tmp_path)
    app = Application(data_root=tmp_path / "data", state_root=tmp_path / "state")
    monkeypatch.setattr(Application, "_definitions", lambda self, config: {"LX001": definition})
    monkeypatch.setattr(Application, "_providers", lambda self, config: {"other": object()})

    with pytest.raises(LabctlError, match="lifecycle is not implemented"):
        app._lab_create(
            parse_args(["--provider", "other", "lab", "create", "LX001", "--trust-external"]),
            Config(provider="other"),
        )

    assert not (tmp_path / "data/instances/LX001").exists()


def test_external_definition_executes_only_the_digest_accepted_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = _external_definition(tmp_path, provider="kvm")
    setup = definition.vms[0].setup
    original = setup.read_text(encoding="utf-8")
    blob = tmp_path / "blob"
    blob.write_bytes(b"image")
    storage = tmp_path / "storage"
    storage.mkdir()
    observed: dict[str, object] = {}

    def mutate_after_acceptance(self: ImageStore, image: str):  # type: ignore[no-untyped-def]
        setup.write_text("#!/bin/sh\ntouch /tmp/tampered\n", encoding="utf-8")
        return blob, "a" * 64, True

    class RecordingOrchestrator:
        def create(self, captured, uri, images, **kwargs):  # type: ignore[no-untyped-def]
            observed["setup"] = captured.vms[0].setup.read_text(encoding="utf-8")
            observed["source"] = captured.source
            return {"id": captured.id, "state": "ready"}

    app = Application(data_root=tmp_path / "data", state_root=tmp_path / "state")
    monkeypatch.setattr(Application, "_definitions", lambda self, config: {"LX001": definition})
    monkeypatch.setattr(Application, "_orchestrator", lambda self, config: RecordingOrchestrator())
    monkeypatch.setattr(ImageStore, "resolve", mutate_after_acceptance)
    monkeypatch.setattr("labctl.kvm.KVMProvider.doctor", lambda self: ProviderHealth(()))

    app._lab_create(
        parse_args(["lab", "create", "LX001", "--trust-external"]),
        Config(libvirt_uri="qemu:///session", libvirt_storage_root=str(storage)),
    )

    assert observed["setup"] == original
    assert Path(observed["source"]).parent != definition.source.parent


def test_application_rejects_private_path_symlink_alias_inside_storage(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    private = storage / "private-data"
    private.mkdir(parents=True)
    data_link = tmp_path / "data-link"
    data_link.symlink_to(private, target_is_directory=True)
    app = Application(
        data_root=data_link,
        state_root=tmp_path / "state",
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        runtime_root=tmp_path / "runtime",
    )

    with pytest.raises(ValueError, match="overlaps private labctl paths"):
        app._libvirt_storage_root(Config(libvirt_storage_root=str(storage)))


def test_builtin_kvm_provider_and_orchestrator_receive_storage_root(tmp_path: Path) -> None:
    data = tmp_path / "custom-data"
    cache = tmp_path / "custom-cache"
    storage = tmp_path / "libvirt-storage"
    storage.mkdir()
    app = Application(data_root=data, cache_root=cache, state_root=tmp_path / "state")

    config = Config(libvirt_uri="qemu:///session", libvirt_storage_root=str(storage))
    provider = app._providers(config)["kvm"]
    orchestrator = app._orchestrator(config)

    assert isinstance(provider, KVMProvider)
    assert provider._storage_paths == (storage,)
    assert orchestrator.storage_root == storage


@pytest.mark.parametrize("unsafe", ["symlink", "overlap"])
def test_application_rejects_unsafe_libvirt_storage_root(tmp_path: Path, unsafe: str) -> None:
    data = tmp_path / "data"
    data.mkdir()
    storage = data if unsafe == "overlap" else tmp_path / "storage-link"
    if unsafe == "symlink":
        target = tmp_path / "storage"
        target.mkdir()
        storage.symlink_to(target, target_is_directory=True)
    app = Application(
        data_root=data,
        state_root=tmp_path / "state",
        cache_root=tmp_path / "cache",
        runtime_root=tmp_path / "runtime",
    )

    with pytest.raises(ValueError, match="libvirt storage root"):
        app._libvirt_storage_root(Config(libvirt_storage_root=str(storage)))
