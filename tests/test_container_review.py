"""Regression checks for connected container outcomes; no container engine is run."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import test_container_labs as catalog


@pytest.mark.parametrize("lab_id", catalog.CT_IDS)
def test_disabled_container_label_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lab_id: str
) -> None:
    values = answers(monkeypatch, lab_id)
    key = next(k for k in values if k.startswith("/usr/bin/podman inspect "))
    items = json.loads(values[key])
    for item in items:
        item["ProcessLabel"] = ""
    values[key] = json.dumps(items)
    result, _ = catalog._run(tmp_path, lab_id, values)
    assert result.returncode == 1


@pytest.mark.parametrize("lab_id", ["CT202", "CT301"])
def test_private_workloads_reject_published_ports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lab_id: str
) -> None:
    values = answers(monkeypatch, lab_id)
    key = next(k for k in values if k.startswith("/usr/bin/podman inspect "))
    items = json.loads(values[key])
    items[0]["HostConfig"]["PortBindings"] = {
        "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]  # noqa: S104 - rejection fixture
    }
    values[key] = json.dumps(items)
    result, _ = catalog._run(tmp_path, lab_id, values)
    assert result.returncode == 1


@pytest.mark.parametrize("lab_id", catalog.CT_IDS)
def test_ssh_exec_format_error_is_a_failed_check(tmp_path: Path, lab_id: str) -> None:
    ssh = tmp_path / "ssh"
    ssh.write_text("not an executable format\n")
    ssh.chmod(0o700)
    result, _ = catalog._run_with_ssh(tmp_path, lab_id, ssh)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert f"FAIL {lab_id}:" in result.stdout


def test_image_payload_not_runtime_patch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    values = answers(monkeypatch, "CT201")
    values["/usr/bin/podman diff ct201-app"] = "C /app/result.txt"
    result, _ = catalog._run(tmp_path, "CT201", values)
    assert result.returncode == 1


@pytest.mark.parametrize("lab_id", catalog.CT_IDS)
def test_nested_null_inspect_fields_never_crash(
    monkeypatch: pytest.MonkeyPatch, lab_id: str
) -> None:
    import copy

    m = module(lab_id)
    validate = getattr(m, "valid", getattr(m, "_valid", None))
    assert validate is not None
    checks = getattr(m, "CHECKS", getattr(m, "COMMANDS", ()))
    values = answers(monkeypatch, lab_id)

    def variants(value: Any) -> Any:
        if isinstance(value, dict):
            for key in value:
                changed = copy.deepcopy(value)
                changed[key] = None
                yield changed
                for child in variants(value[key]):
                    changed = copy.deepcopy(value)
                    changed[key] = child
                    yield changed
        elif isinstance(value, list):
            for index, item in enumerate(value):
                changed = copy.deepcopy(value)
                changed[index] = None
                yield changed
                for child in variants(item):
                    changed = copy.deepcopy(value)
                    changed[index] = child
                    yield changed

    for _description, command, kind in checks:
        try:
            value = json.loads(values[command])
        except (ValueError, KeyError):
            continue
        for changed in variants(value):
            assert isinstance(validate(kind, json.dumps(changed)), bool)


@pytest.mark.parametrize("lab_id", catalog.CT_IDS)
def test_definition_and_guest_contracts_are_identical(lab_id: str) -> None:
    from labctl.definitions import load_definition

    setup = (catalog.LABS / lab_id / "setup.sh").read_text()
    contract = setup.split("/home/student/LAB.md <<'EOF'\n", 1)[1].split("\nEOF", 1)[0]
    definition = load_definition(catalog.LABS / lab_id / "lab.yaml")
    assert definition.instructions.strip() == contract.strip()


def module(lab_id: str) -> ModuleType:
    # Do not write bytecode beside snapshot assets: every adjacent file is trusted input.
    path = catalog.LABS / lab_id / "grade.py"
    result = ModuleType(lab_id)
    result.__file__ = str(path)
    exec(compile(path.read_text(), str(path), "exec"), result.__dict__)  # noqa: S102
    return result


def answers(monkeypatch: pytest.MonkeyPatch, lab_id: str) -> dict[str, Any]:
    saved = {}

    def capture(_path: Path, _id: str, _title: str, values: dict[str, Any]) -> None:
        saved.update(values)

    monkeypatch.setattr(catalog, "_assert_lab", capture)
    names = {
        "CT001": "one_shot_container",
        "CT002": "lifecycle_logs_and_exec",
        "CT101": "web_port",
        "CT102": "named_volume",
        "CT201": "containerfile_build",
        "CT202": "private_network",
        "CT301": "pod_and_config",
        "CT302": "effective_hardening",
        "CT401": "quadlet_reboot_persistence",
        "CT402": "incident_recovery",
    }
    getattr(catalog, f"test_{lab_id.lower()}_{names[lab_id]}")(Path("unused"))
    return saved


@pytest.mark.parametrize("lab_id", catalog.CT_IDS)
def test_transport_failure_timeout_and_malformed_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lab_id: str
) -> None:
    m = module(lab_id)
    validate = getattr(m, "valid", getattr(m, "_valid", None))
    assert validate is not None
    for text in ("null", "[]", "{}", "[null]", '{"Mounts":[null]}', '"text"'):
        assert not validate("inspect", text)
    values = answers(monkeypatch, lab_id)
    for command in values:
        failed = dict(values)
        failed[command] = {"returncode": 255, "stdout": values[command]}
        result, _ = catalog._run(tmp_path / str(len(list((tmp_path).iterdir()))), lab_id, failed)
        assert result.returncode != 0
    # Exercise the real production catch branch without sleeping for 15 seconds.
    context_dir = tmp_path / "timeout"
    catalog._run(context_dir, lab_id, values)
    monkeypatch.setattr(m.sys, "argv", [str(m.__file__), str(context_dir / "context.json")])
    monkeypatch.setenv("LABCTL_SSH", str(context_dir / "ssh"))

    def timeout(*_args: Any, **kwargs: Any) -> Any:
        assert kwargs["timeout"] == 15
        raise subprocess.TimeoutExpired("ssh", 15)

    monkeypatch.setattr(m.subprocess, "run", timeout)
    assert m.main() == 1


def test_restarted_worker_logs_are_accepted() -> None:
    m = module("CT002")
    assert m.valid("log", "CT002 worker ready\nCT002 worker ready\n")


@pytest.mark.parametrize("lab_id", ["CT101", "CT402"])
def test_published_port_is_probed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lab_id: str
) -> None:
    values = answers(monkeypatch, lab_id)
    # A working container-local endpoint does not prove host forwarding works.
    values[
        "/usr/bin/curl --noproxy '*' --max-time 5 -fsS http://127.0.0.1:8080/"
        + ("index.html" if lab_id == "CT101" else "health")
    ] = {"returncode": 7}
    result, _ = catalog._run(tmp_path, lab_id, values)
    assert result.returncode == 1


def test_image_id_and_mounts_are_connected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    values = answers(monkeypatch, "CT201")
    command = "/usr/bin/podman inspect ct201-app"
    item = json.loads(values[command])[0]
    item["Image"] = "b" * 64
    values[command] = json.dumps([item])
    result, _ = catalog._run(tmp_path, "CT201", values)
    assert result.returncode == 1


def test_pod_real_array_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    values = answers(monkeypatch, "CT301")
    raw = json.loads(values["/usr/bin/podman pod inspect ct301-stack"])
    if isinstance(raw, dict):
        raw = [raw]
    assert module("CT301").valid("pod", json.dumps(raw))


@pytest.mark.parametrize("lab_id", ["CT201", "CT202", "CT301", "CT401"])
def test_cross_checks_exist(lab_id: str) -> None:
    m = module(lab_id)
    assert callable(getattr(m, "connected", None)), "independent checks cannot prove ownership"
    assert m.connected({}) is False


def test_effective_resources_and_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    values = answers(monkeypatch, "CT302")
    item = json.loads(values["/usr/bin/podman inspect ct302-secure"])[0]
    item["EffectiveCaps"] = []
    item["BoundingCaps"] = []
    item["HostConfig"]["CapDrop"] = ["CAP_CHOWN", "CAP_SETUID"]
    item["HostConfig"]["NanoCpus"] = 0
    item["HostConfig"]["CpuPeriod"] = 200000
    item["HostConfig"]["CpuQuota"] = 100000
    assert module("CT302").valid("inspect", json.dumps([item]))
    assert module("CT302").valid(
        "probe",
        "10001\n10001\nro\nNoNewPrivs:\t1\nCapEff:\t0000000000000000\n"
        "CapBnd:\t0000000000000000\n268435456\n64\n50000 100000\n",
    )


@pytest.mark.parametrize("lab_id", catalog.CT_IDS)
def test_setup_mapping_and_user_manager_contract(lab_id: str) -> None:
    setup = (catalog.LABS / lab_id / "setup.sh").read_text()
    assert "def ensure_subids" in setup
    assert 'systemctl start "user@$uid.service"' in setup
    assert "HOME=/home/student" in setup
    assert "DBUS_SESSION_BUS_ADDRESS=" in setup
    assert "--pull=never" in setup or "podman image exists" in setup


def test_quadlet_contract_explains_boot_dependency() -> None:
    setup = (catalog.LABS / "CT401/setup.sh").read_text()
    for text in (
        "WantedBy=default.target",
        "Pull=never",
        "[Install]",
        "systemctl --user daemon-reload",
    ):
        assert text in setup


def test_subid_setup_is_idempotent_and_rejects_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ast

    text = (catalog.LABS / "CT001/setup.sh").read_text()
    script = text.split("<<'CT_SUBIDS'\n", 1)[1].split("\nCT_SUBIDS", 1)[0]
    tree = ast.parse(script)
    tree.body = [node for node in tree.body if not isinstance(node, ast.Expr)]
    namespace: dict[str, Any] = {}
    exec(compile(tree, "setup-subids", "exec"), namespace)  # noqa: S102
    mapping = tmp_path / "subuid"
    mapping.write_text("another:100000:65536\n")
    calls = []

    def usermod(argv: list[str], **kwargs: Any) -> None:
        assert kwargs == {"check": True}
        calls.append(argv)
        lo, hi = map(int, argv[2].split("-"))
        with mapping.open("a") as stream:
            stream.write(f"student:{lo}:{hi - lo + 1}\n")

    monkeypatch.setattr(namespace["subprocess"], "run", usermod)
    namespace["ensure_subids"](mapping, "--add-subuids")
    namespace["ensure_subids"](mapping, "--add-subuids")
    assert len(calls) == 1
    assert calls[0][2] == "165536-231071"
    for content in ("student:100000:10\n", "student:100000:65536\nother:110000:65536\n"):
        mapping.write_text(content)
        with pytest.raises(RuntimeError):
            namespace["ensure_subids"](mapping, "--add-subuids")


@pytest.mark.parametrize("lab_id", ["CT101", "CT402"])
def test_loopback_publication_rejects_extra_ports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lab_id: str
) -> None:
    values = answers(monkeypatch, lab_id)
    key = next(k for k in values if k.startswith("/usr/bin/podman inspect "))
    items = json.loads(values[key])
    items[0]["HostConfig"]["PortBindings"]["9090/tcp"] = [
        {"HostIp": "0.0.0.0", "HostPort": "9090"}  # noqa: S104
    ]
    values[key] = json.dumps(items)
    result, _ = catalog._run(tmp_path, lab_id, values)
    assert result.returncode == 1


@pytest.mark.parametrize("content", ["", "wrong\n"])
def test_pod_rejects_wrong_mounted_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str
) -> None:
    values = answers(monkeypatch, "CT301")
    values["/usr/bin/podman exec ct301-web cat /etc/ct301/app.conf"] = content
    result, _ = catalog._run(tmp_path, "CT301", values)
    assert result.returncode == 1


def test_pod_rejects_unconnected_service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    values = answers(monkeypatch, "CT301")
    # Constant HTTP is correct but no listener/docroot ownership proof succeeds.
    for _desc, command, kind in module("CT301").CHECKS:
        if kind == "service":
            values[command] = {"returncode": 1}
    result, _ = catalog._run(tmp_path, "CT301", values)
    assert result.returncode == 1


@pytest.mark.parametrize("lab_id,destination", [("CT301", "/etc/ct301"), ("CT402", "/srv/health")])
def test_service_rejects_shadow_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lab_id: str, destination: str
) -> None:
    values = answers(monkeypatch, lab_id)
    key = next(k for k in values if k.startswith("/usr/bin/podman inspect "))
    items = json.loads(values[key])
    items[0]["Mounts"].append({"Type": "tmpfs", "Destination": destination})
    values[key] = json.dumps(items)
    result, _ = catalog._run(tmp_path, lab_id, values)
    assert result.returncode == 1


def test_capstone_rejects_erased_original_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    values = answers(monkeypatch, "CT402")
    values["/usr/bin/podman exec ct402-api cat /srv/health"] = {"returncode": 1}
    for _desc, command, kind in module("CT402").CHECKS:
        if kind == "service":
            values[command] = {"returncode": 1}
    result, _ = catalog._run(tmp_path, "CT402", values)
    assert result.returncode == 1


def test_capstone_detects_recreated_volume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    values = answers(monkeypatch, "CT402")
    values["/usr/bin/podman volume inspect ct402-data"] = json.dumps(
        [{"Name": "ct402-data", "CreatedAt": "new"}]
    )
    result, _ = catalog._run(tmp_path, "CT402", values)
    assert result.returncode == 1
