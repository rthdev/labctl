import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from labctl.definitions import DefinitionError, load_definition

LABS = Path(__file__).parents[1] / "src/labctl/data/labs"

GROUPS = {
    "AN001": {"target": ("web",)},
    "AN002": {"target": ("managed",)},
    "AN101": {"target": ("managed",)},
    "AN102": {"target": ("web",)},
    "AN201": {"app": ("app_servers",), "proxy": ("proxies",)},
    "AN202": {"target": ("storage_web",)},
    "AN301": {name: ("web_fleet",) for name in ("web1", "web2", "web3")},
    "AN302": {"target": ("identity_targets",)},
    "AN401": {"target": ("app",)},
    "AN402": {name: ("platform",) for name in ("web1", "web2", "web3")},
}


def test_catalogue_explicit_access():
    found = {}
    for path in LABS.glob("*/lab.yaml"):
        definition = load_definition(path)
        access = definition.controller_access
        if definition.id in GROUPS:
            assert access is not None
            assert access.controller == "controller"
            found[definition.id] = dict(access.targets)
        else:
            assert access is None
    assert found == GROUPS


def definition_file(tmp_path, access, *, lab_id="AN999"):
    data = yaml.safe_load((LABS / "AN001/lab.yaml").read_text())
    data["id"] = lab_id
    data["controller_access"] = access
    for name in ("grade.py", "setup-controller.sh", "setup-target.sh"):
        path = tmp_path / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o700)
    path = tmp_path / "lab.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_explicit_controller_access_metadata(tmp_path):
    path = definition_file(tmp_path, {"controller": "controller", "targets": {"target": ["web"]}})
    definition = load_definition(path)
    assert definition.controller_access.controller == "controller"
    assert definition.controller_access.targets == (("target", ("web",)),)


@pytest.mark.parametrize(
    "access",
    [
        None,
        {},
        {"controller": "controller", "targets": {}},
        {"controller": "missing", "targets": {"target": ["web"]}},
        {"controller": "controller", "targets": {"controller": ["web"]}},
        {"controller": "controller", "targets": {"missing": ["web"]}},
        {"controller": "controller", "targets": {"target": ["web\n[all:vars]"]}},
        {"controller": "controller", "targets": {"target": ["all"]}},
        {"controller": "controller", "targets": {"target": ["web", "web"]}},
        {"controller": "controller", "targets": {"target": []}},
        {"controller": "controller", "targets": {"target": ["web"]}, "path": "/etc"},
    ],
)
def test_invalid_access_metadata(tmp_path, access):
    with pytest.raises(DefinitionError, match="controller_access"):
        load_definition(definition_file(tmp_path, access))


def test_access_requires_ansible_scope(tmp_path):
    with pytest.raises(DefinitionError, match="controller_access"):
        load_definition(
            definition_file(
                tmp_path,
                {"controller": "controller", "targets": {"target": ["web"]}},
                lab_id="LX999",
            )
        )


def guest(script, home):
    return subprocess.run(
        [sys.executable, "-c", script.replace("/home/student", str(home))],
        capture_output=True,
        text=True,
    )


def test_guest_key_is_generated_and_persistent(tmp_path):
    from labctl import controller_access

    script = controller_access.key_script()
    first = guest(script, tmp_path)
    assert first.returncode == 0, first.stderr
    key = tmp_path / ".ssh/labctl-practice/id_ed25519"
    assert key.stat().st_mode & 0o777 == 0o600
    before = key.read_bytes()
    second = guest(script, tmp_path)
    assert second.returncode == 0, second.stderr
    assert second.stdout == first.stdout
    assert first.stdout.startswith("ssh-ed25519 ")
    assert "PRIVATE" not in first.stdout
    assert key.read_bytes() == before


@pytest.mark.parametrize(
    "component", [".ssh", ".ssh/labctl-practice", ".ssh/labctl-practice/id_ed25519"]
)
def test_key_refuses_symlinks(tmp_path, component):
    from labctl import controller_access

    home = tmp_path / "home"
    home.mkdir()
    destination = tmp_path / "untouched"
    destination.mkdir()
    path = home / component
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(destination)
    assert guest(controller_access.key_script(), home).returncode != 0
    assert list(destination.iterdir()) == []


def test_refresh_scripts_preserve_work_and_parse_with_openssh(tmp_path):
    from labctl import controller_access as access

    definition = load_definition(LABS / "AN001/lab.yaml")
    public = guest(access.key_script(), tmp_path).stdout.strip()
    hosts = {"target": {"address": "192.0.2.10", "host_public_key": public}}
    project = tmp_path / "ansible-lab"
    project.mkdir()
    (project / "site.yml").write_text("learner work")
    config = tmp_path / ".ssh/config"
    config.write_text("Host elsewhere\n  User another\n")
    script = access.controller_script(definition.controller_access, hosts)
    for _ in range(2):
        result = guest(script, tmp_path)
        assert result.returncode == 0, result.stderr
    assert (project / "site.yml").read_text() == "learner work"
    assert config.read_text().count("Host elsewhere") == 1
    inventory = (project / "inventory.ini").read_text()
    assert "[web]" in inventory
    assert "target ansible_host=192.0.2.10" in inventory
    parsed = subprocess.run(
        ["/usr/bin/ssh", "-G", "-F", str(config), "target"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in (
        "hostname 192.0.2.10",
        "user student",
        "stricthostkeychecking true",
        "identitiesonly yes",
    ):
        assert line in parsed
    assert str(tmp_path / ".ssh/labctl-practice/id_ed25519") in parsed
    target = tmp_path / "target"
    target.mkdir()
    (target / ".ssh").mkdir()
    authorized = target / ".ssh/authorized_keys"
    authorized.write_text("host-management-key\n")
    for _ in range(2):
        result = guest(access.authorize_script(public), target)
        assert result.returncode == 0, result.stderr
    assert authorized.read_text().count(public) == 1
    assert "host-management-key" in authorized.read_text()


@pytest.mark.parametrize(
    "value",
    [
        "ssh-ed25519 AAAA",
        "ssh-ed25519 AAAA\nssh-rsa AAAA",
        "ssh-rsa AAAA",
        "options ssh-ed25519 AAAA",
    ],
)
def test_public_keys_reject_malformed_wire_data(value):
    from labctl import controller_access as access

    with pytest.raises(ValueError, match="key"):
        access.authorize_script(value)


def provision(tmp_path, *, enabled=True, fail_remote=False):
    from test_orchestrator import FakeRunner, _image, _keys

    from labctl.images import ImageStore
    from labctl.orchestrator import KVMOrchestrator
    from labctl.ssh import generate_keypair
    from labctl.subprocesses import CommandResult

    class PracticeRunner(FakeRunner):
        def __init__(self):
            super().__init__()
            self.power = {}
            self.addresses = {}
            self.remote = []
            self.fail_remote = False

        def run(self, argv, *, check=True):
            if "ssh" in argv:
                self.calls.append(tuple(argv))
                if self.fail_remote:
                    return CommandResult(tuple(argv), 1, "", "failed guest command")
                destination = argv[argv.index("--") + 1].split("@")[1]
                domain = next(d for d, a in self.addresses.items() if a == destination)
                name = domain.rsplit("-", 1)[1]
                self.remote.append(name)
                home = tmp_path / "guests" / name
                home.mkdir(parents=True, exist_ok=True)
                command = shlex.split(argv[-1])
                assert command[:2] == ["/usr/bin/python3", "-c"]
                result = guest(command[2], home)
                return CommandResult(tuple(argv), result.returncode, result.stdout, result.stderr)
            if argv[0] == "qemu-img":
                Path(argv[-2]).write_bytes(b"overlay")
            if argv[0] == "cloud-localds":
                Path(argv[1]).write_bytes(b"seed")
            if argv[0] == "virt-install":
                domain = argv[argv.index("--name") + 1]
                self.power[domain] = "running"
                self.addresses.setdefault(domain, f"192.0.2.{10 + len(self.addresses)}")
            command_argv = argv[3:] if argv[:2] == ["timeout", "--signal=KILL"] else argv
            if command_argv[0] == "virsh":
                op, domain = command_argv[3:5]
                if op in ("shutdown", "destroy"):
                    self.power[domain] = "shut off"
                if op == "start":
                    self.power[domain] = "running"
                if op == "domstate":
                    return CommandResult(tuple(argv), 0, self.power[domain], "")
                if op == "domifaddr":
                    return CommandResult(tuple(argv), 0, f"ipv4 {self.addresses[domain]}/24", "")
            return super().run(argv, check=check)

    source = tmp_path / "source"
    source.mkdir()
    path = definition_file(source, {"controller": "controller", "targets": {"target": ["web"]}})
    data = yaml.safe_load(path.read_text())
    if not enabled:
        data.pop("controller_access")
    for vm in data["vms"]:
        vm["image"] = "local:test"
    path.write_text(yaml.safe_dump(data))
    runner = PracticeRunner()
    runner.fail_remote = fail_remote
    images = ImageStore(tmp_path / "images", runner=runner)
    _image(images)
    orchestrator = KVMOrchestrator(
        tmp_path / "data",
        tmp_path / "state",
        runner=runner,
        keygen=_keys,
        host_keygen=generate_keypair,
    )
    state = orchestrator.create(
        load_definition(path), "qemu:///system", images, readiness_probe=lambda *_: True
    )
    return orchestrator, runner, images, state


def test_create_and_lifecycle_refresh_practice_access(tmp_path):
    orchestrator, runner, images, state = provision(tmp_path)
    project = tmp_path / "guests/controller/ansible-lab"
    inventory = project / "inventory.ini"
    assert inventory.is_file()
    assert set(runner.remote) == {"controller", "target"}
    (project / "site.yml").write_text("learner playbook")
    key = tmp_path / "guests/controller/.ssh/labctl-practice/id_ed25519"
    original = key.read_bytes()
    target_domain = state["vms"]["target"]["domain"]
    for operation in ("start", "restart", "reset"):
        runner.addresses[target_domain] = {
            "start": "192.0.2.20",
            "restart": "192.0.2.21",
            "reset": "192.0.2.22",
        }[operation]
        if operation == "reset":
            state = orchestrator.reset(
                "AN999", images, vm_names=("target",), readiness_probe=lambda *_: True
            )
        elif operation == "restart":
            state = orchestrator.restart(
                "AN999", force=True, vm_name="target", readiness_probe=lambda *_: True
            )
        else:
            state = orchestrator.start("AN999", readiness_probe=lambda *_: True)
        assert runner.addresses[target_domain] in inventory.read_text()
        assert state["vms"]["target"]["address"] == runner.addresses[target_domain]
        assert (project / "site.yml").read_text() == "learner playbook"
        assert key.read_bytes() == original


def test_refresh_failure_is_not_success(tmp_path):
    from labctl.orchestrator import OrchestrationError

    orchestrator, runner, _, _ = provision(tmp_path)
    runner.fail_remote = True
    with pytest.raises(OrchestrationError, match="practice"):
        orchestrator.start("AN999", readiness_probe=lambda *_: True)


@pytest.mark.parametrize(
    "address",
    ["-oProxyCommand=evil", "target\nHost *", "192.0.2.1 bad", "fe80::1%eth0", "::1%\nHost *"],
)
def test_reject_unsafe_addresses(tmp_path, address):
    from labctl import controller_access as access

    public = guest(access.key_script(), tmp_path).stdout.strip()
    definition = load_definition(LABS / "AN001/lab.yaml")
    with pytest.raises(ValueError):
        access.controller_script(
            definition.controller_access,
            {"target": {"address": address, "host_public_key": public}},
        )


@pytest.mark.parametrize(
    "component",
    [
        "ansible-lab",
        "ansible-lab/inventory.ini",
        ".ssh/config",
        ".ssh/labctl-practice/config",
        ".ssh/labctl-practice/known_hosts",
        ".ssh/authorized_keys",
    ],
)
def test_refresh_rejects_symlinks_without_touching_destination(tmp_path, component):
    from labctl import controller_access as access

    home = tmp_path / "home"
    home.mkdir()
    public = guest(access.key_script(), home).stdout.strip()
    destination = tmp_path / "untouched"
    destination.write_text("untouched")
    path = home / component
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(destination)
    definition = load_definition(LABS / "AN001/lab.yaml")
    script = (
        access.authorize_script(public)
        if component.endswith("authorized_keys")
        else access.controller_script(
            definition.controller_access,
            {"target": {"address": "192.0.2.10", "host_public_key": public}},
        )
    )
    assert guest(script, home).returncode != 0
    assert destination.read_text() == "untouched"


def test_stopped_controller_defers_without_starting_it(tmp_path):
    orchestrator, runner, images, state = provision(tmp_path)
    domain = state["vms"]["controller"]["domain"]
    runner.power[domain] = "shut off"
    runner.remote.clear()
    state = orchestrator.reset(
        "AN999", images, vm_names=("target",), readiness_probe=lambda *_: True
    )
    assert runner.power[domain] == "shut off"
    assert runner.remote == []
    assert state["controller_access_status"] == "pending"
    state = orchestrator.start("AN999", vm_name="controller", readiness_probe=lambda *_: True)
    assert state["controller_access_status"] == "ready"


def test_altered_snapshot_refuses_refresh(tmp_path):
    from labctl.orchestrator import OrchestrationError

    orchestrator, runner, _, _ = provision(tmp_path)
    snapshot = tmp_path / "data/instances/AN999/definition/lab.yaml"
    snapshot.write_text(snapshot.read_text() + "# altered\n")
    runner.remote.clear()
    with pytest.raises(OrchestrationError, match="snapshot"):
        orchestrator.start("AN999", readiness_probe=lambda *_: True)
    assert runner.remote == []


@pytest.mark.parametrize("lab_id", GROUPS)
def test_all_inventories_with_real_ansible_parser(tmp_path, lab_id):
    import json
    import shutil

    from labctl import controller_access as access

    executable = shutil.which("ansible-inventory")
    if executable is None:
        pytest.skip("ansible-inventory is not installed")
    public = guest(access.key_script(), tmp_path).stdout.strip()
    definition = load_definition(LABS / lab_id / "lab.yaml")
    hosts = {
        name: {"address": f"192.0.2.{index + 10}", "host_public_key": public}
        for index, name in enumerate(GROUPS[lab_id])
    }
    result = guest(access.controller_script(definition.controller_access, hosts), tmp_path)
    assert result.returncode == 0, result.stderr
    parsed = subprocess.run(
        [executable, "-i", str(tmp_path / "ansible-lab/inventory.ini"), "--list"],
        capture_output=True,
        text=True,
        check=True,
    )
    inventory = json.loads(parsed.stdout)
    for name, groups in GROUPS[lab_id].items():
        for group in groups:
            assert name in inventory[group]["hosts"]
        assert inventory["_meta"]["hostvars"][name]["ansible_host"] == hosts[name]["address"]


def test_replacing_controller_key_revokes_previous_practice_key(tmp_path):
    from labctl import controller_access as access

    public = guest(access.key_script(), tmp_path).stdout.strip()
    result = guest(access.authorize_script(public), tmp_path)
    assert result.returncode == 0, result.stderr
    other = tmp_path / "other"
    other.mkdir()
    replacement = guest(access.key_script(), other).stdout.strip()
    result = guest(access.authorize_script(replacement), tmp_path)
    assert result.returncode == 0, result.stderr
    authorized = (tmp_path / ".ssh/authorized_keys").read_text()
    assert replacement in authorized
    assert public not in authorized


@pytest.mark.parametrize("mutation", ["hardlink", "permissions", "corrupt"])
def test_unsafe_existing_private_key_is_not_replaced(tmp_path, mutation):
    from labctl import controller_access as access

    assert guest(access.key_script(), tmp_path).returncode == 0
    key = tmp_path / ".ssh/labctl-practice/id_ed25519"
    if mutation == "hardlink":
        (tmp_path / "link").hardlink_to(key)
    elif mutation == "permissions":
        key.chmod(0o644)
    else:
        key.write_text("not a private key")
    before = key.read_bytes()
    assert guest(access.key_script(), tmp_path).returncode != 0
    assert key.read_bytes() == before


def test_write_failure_does_not_replace_learner_files(tmp_path):
    from labctl import controller_access as access

    public = guest(access.key_script(), tmp_path).stdout.strip()
    definition = load_definition(LABS / "AN001/lab.yaml")
    script = access.controller_script(
        definition.controller_access,
        {"target": {"address": "192.0.2.1", "host_public_key": public}},
    )
    project = tmp_path / "ansible-lab"
    project.mkdir()
    (project / "site.yml").write_text("work")
    script = script.replace(
        "os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)",
        "pass # simulate a lost write",
    )
    assert guest(script, tmp_path).returncode != 0
    assert (project / "site.yml").read_text() == "work"


def test_old_ansible_snapshots_do_not_implicitly_enable_access(tmp_path):
    orchestrator, runner, images, _ = provision(tmp_path, enabled=False)
    orchestrator.start("AN999", readiness_probe=lambda *_: True)
    orchestrator.restart("AN999", force=True, readiness_probe=lambda *_: True)
    orchestrator.reset("AN999", images, readiness_probe=lambda *_: True)
    assert runner.remote == []
    assert not (tmp_path / "guests").exists()


def test_create_access_failure_rolls_back(tmp_path):
    from labctl.orchestrator import OrchestrationError

    with pytest.raises(OrchestrationError, match="practice"):
        provision(tmp_path, fail_remote=True)
    assert not (tmp_path / "state/labs/AN999.json").exists()


def test_reset_access_failure_restores_target_overlay_preserves_controller(tmp_path):
    from labctl.orchestrator import OrchestrationError

    orchestrator, runner, images, state = provision(tmp_path)
    overlay = Path(state["vms"]["target"]["overlay"])
    overlay.write_bytes(b"original target")
    project = tmp_path / "guests/controller/ansible-lab/site.yml"
    project.write_text("work")
    runner.fail_remote = True
    with pytest.raises(OrchestrationError, match="practice"):
        orchestrator.reset("AN999", images, vm_names=("target",), readiness_probe=lambda *_: True)
    assert overlay.read_bytes() == b"original target"
    assert project.read_text() == "work"


def model_reset_guest_disk(monkeypatch, overlay, home):
    """Move the guest home with its disk; rollback restores the old key too."""
    replace = Path.replace
    backup = overlay.with_suffix(".qcow2.reset-backup")
    saved_home = home.with_name(home.name + "-disk-backup")

    def replace_disk(source, destination):
        result = replace(source, destination)
        if source == overlay and destination == backup:
            replace(home, saved_home)
        elif source == backup and destination == overlay:
            if home.exists():
                shutil.rmtree(home)
            replace(saved_home, home)
        return result

    monkeypatch.setattr(Path, "replace", replace_disk)


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("failure_stage", ["publish", "persist"])
def test_controller_reset_late_failure_recovers_pending_then_repairs_access(
    tmp_path, monkeypatch, interrupted, failure_stage
):
    from labctl import controller_access as access
    from labctl import orchestrator as module
    from labctl.orchestrator import OrchestrationError
    from labctl.state import load_state

    orchestrator, runner, images, state = provision(tmp_path)
    home = tmp_path / "guests/controller"
    target = tmp_path / "guests/target"
    key = home / ".ssh/labctl-practice/id_ed25519"
    original_key = key.read_bytes()
    original_public = guest(access.key_script(), home).stdout.strip()
    project = home / "ansible-lab/site.yml"
    project.write_text("learner work")
    target_work = target / "learner-work"
    target_work.write_text("unselected work")
    overlay = Path(state["vms"]["controller"]["overlay"])
    model_reset_guest_disk(monkeypatch, overlay, home)
    remote = orchestrator._practice_remote
    replacement_keys = []

    def fail():
        if interrupted:
            raise KeyboardInterrupt("simulated process interruption")
        raise OrchestrationError("practice publish failed")

    def fail_publish(vm, script, *, operation):
        if "inventory.ini" in script:
            replacement = guest(access.key_script(), home).stdout.strip()
            assert replacement != original_public
            assert replacement in (target / ".ssh/authorized_keys").read_text()
            assert original_public not in (target / ".ssh/authorized_keys").read_text()
            replacement_keys.append(replacement)
            if failure_stage == "publish":
                fail()
        return remote(vm, script, operation=operation)

    save = module.save_state
    failed_save = False

    def fail_persist(path, current):
        nonlocal failed_save
        if failure_stage == "persist" and replacement_keys and not failed_save:
            assert current["controller_access_status"] == "ready"
            failed_save = True
            fail()
        return save(path, current)

    monkeypatch.setattr(module, "save_state", fail_persist)
    monkeypatch.setattr(orchestrator, "_practice_remote", fail_publish)
    runner.calls.clear()
    error = KeyboardInterrupt if interrupted else OrchestrationError
    with pytest.raises(error):
        orchestrator.reset(
            "AN999", images, vm_names=("controller",), readiness_probe=lambda *_: True
        )
    assert len(replacement_keys) == 1
    assert load_state(orchestrator._state_path("AN999"))["controller_access_status"] == "pending"
    journal_path = orchestrator._journal_path("AN999")
    journal = json.loads(journal_path.read_text())
    assert set(journal["vms"]) == {"controller"}
    recovered, committed = orchestrator._recover_reset(journal)
    assert not committed
    assert key.read_bytes() == original_key
    assert project.read_text() == "learner work"
    assert original_public not in (target / ".ssh/authorized_keys").read_text()
    assert recovered["controller_access_status"] == "pending"
    assert load_state(orchestrator._state_path("AN999"))["controller_access_status"] == "pending"
    assert not journal_path.exists()

    monkeypatch.setattr(orchestrator, "_practice_remote", remote)
    repaired = orchestrator.start("AN999", vm_name="controller", readiness_probe=lambda *_: True)
    assert repaired["controller_access_status"] == "ready"
    authorized = (target / ".ssh/authorized_keys").read_text()
    assert original_public in authorized
    assert replacement_keys[0] not in authorized
    assert key.read_bytes() == original_key
    assert target_work.read_text() == "unselected work"
    assert state["vms"]["target"]["address"] in (home / "ansible-lab/inventory.ini").read_text()
    parsed = subprocess.run(
        ["/usr/bin/ssh", "-G", "-F", str(home / ".ssh/config"), "target"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"hostname {state['vms']['target']['address']}" in parsed
    assert str(key) in parsed
    target_domain = state["vms"]["target"]["domain"]
    assert not any(
        call[0] == "virsh"
        and call[3] in ("shutdown", "destroy", "undefine", "start")
        and target_domain in call
        for call in runner.calls
    )
    assert all(
        call[call.index("--name") + 1] != target_domain
        for call in runner.calls
        if call[0] == "virt-install"
    )


@pytest.mark.parametrize(
    ("marker", "vm_name", "operation"),
    [
        ("ssh-keygen", "controller", "prepare key"),
        ("authorized_keys", "target", "authorize key"),
        ("inventory.ini", "controller", "publish connections"),
    ],
)
def test_practice_failure_identifies_vm_and_operation_without_guest_output(
    tmp_path, monkeypatch, marker, vm_name, operation
):
    from labctl.orchestrator import OrchestrationError
    from labctl.subprocesses import CommandResult

    orchestrator, runner, _, _ = provision(tmp_path)
    run = runner.run

    def fail(argv, *, check=True):
        if "ssh" in argv and marker in argv[-1]:
            return CommandResult(tuple(argv), 1, "sensitive stdout", "sensitive stderr")
        return run(argv, check=check)

    monkeypatch.setattr(runner, "run", fail)
    with pytest.raises(OrchestrationError) as error:
        orchestrator.start("AN999", readiness_probe=lambda *_: True)
    assert vm_name in str(error.value)
    assert operation in str(error.value)
    assert "sensitive" not in str(error.value)


def test_unsafe_persisted_known_hosts_refused_before_refresh(tmp_path):
    from labctl.orchestrator import OrchestrationError

    orchestrator, runner, _, state = provision(tmp_path)
    known = Path(state["vms"]["target"]["known_hosts"])
    destination = tmp_path / "untouched"
    destination.write_text("untouched")
    known.unlink()
    known.symlink_to(destination)
    runner.remote.clear()
    with pytest.raises(OrchestrationError):
        orchestrator.start("AN999", readiness_probe=lambda *_: True)
    assert runner.remote == []
    assert destination.read_text() == "untouched"
