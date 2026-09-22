"""Strict loading and discovery of versioned declarative lab definitions."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

LAB_ID = re.compile(r"^[A-Z]{2}[0-9]{3}$")
VM_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
SIZE = re.compile(r"^([1-9][0-9]*)(KiB|MiB|GiB|TiB)$")
_UNITS = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}


class DefinitionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Interface:
    type: str


@dataclass(frozen=True, slots=True)
class VMDefinition:
    name: str
    cpus: int
    ram: str
    disk: str
    image: str
    hostname: str
    interfaces: tuple[Interface, ...]
    setup: Path
    depends_on: tuple[str, ...]
    ssh_user: str | None = None

    @property
    def ram_bytes(self) -> int:
        return parse_size(self.ram)


@dataclass(frozen=True, slots=True)
class GradingDefinition:
    reset_vms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LabDefinition:
    schema_version: int
    id: str
    title: str
    goal: str
    instructions: str
    provider: str
    grader: Path
    vms: tuple[VMDefinition, ...]
    source: Path
    grading: GradingDefinition = GradingDefinition()
    external: bool = False

    def dependency_order(self) -> tuple[VMDefinition, ...]:
        ordered: list[VMDefinition] = []
        by_name = {vm.name: vm for vm in self.vms}
        active: list[str] = []
        done: set[str] = set()

        def visit(vm: VMDefinition) -> None:
            if vm.name in active:
                cycle = " -> ".join([*active[active.index(vm.name) :], vm.name])
                raise DefinitionError(f"dependency cycle: {cycle}")
            if vm.name in done:
                return
            active.append(vm.name)
            for dependency in vm.depends_on:
                visit(by_name[dependency])
            active.pop()
            done.add(vm.name)
            ordered.append(vm)

        for item in self.vms:
            visit(item)
        return tuple(ordered)


def parse_size(value: str) -> int:
    match = SIZE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise DefinitionError("size must be a positive KiB, MiB, GiB, or TiB value")
    return int(match.group(1)) * _UNITS[match.group(2)]


def _mapping(value: Any, path: str, allowed: set[str], required: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise DefinitionError(f"{path or '<root>'}: must be a mapping")
    unknown = value.keys() - allowed
    missing = required - value.keys()
    if unknown:
        raise DefinitionError(f"{path}{'.' if path else ''}{sorted(unknown)[0]}: unknown field")
    if missing:
        raise DefinitionError(f"{path}{'.' if path else ''}{sorted(missing)[0]}: required field")
    return value


def _script(root: Path, value: Any, path: str) -> Path:
    if not isinstance(value, str):
        raise DefinitionError(f"{path}: must be a path")
    candidate = root / value
    if candidate.is_symlink():
        raise DefinitionError(f"{path}: symlink scripts are forbidden")
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise DefinitionError(f"{path}: path escapes definition directory") from exc
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise DefinitionError(f"{path}: must be a regular executable file")
    return candidate


def load_definition(source: Path, *, external: bool = False) -> LabDefinition:
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DefinitionError(f"{source}: {exc}") from exc
    root_fields = {
        "schema_version",
        "id",
        "title",
        "goal",
        "instructions",
        "provider",
        "grader",
        "grading",
        "vms",
    }
    data = _mapping(raw, "", root_fields, root_fields - {"grading"})
    if data["schema_version"] != 1:
        raise DefinitionError(f"{source}: schema_version must be 1")
    if not isinstance(data["id"], str) or not LAB_ID.fullmatch(data["id"]):
        raise DefinitionError(f"{source}: id must match {LAB_ID.pattern}")
    for field in ("title", "goal", "instructions", "provider"):
        if not isinstance(data[field], str) or not data[field].strip():
            raise DefinitionError(f"{source}: {field} must be a non-empty string")
    if not isinstance(data["vms"], list) or not data["vms"]:
        raise DefinitionError(f"{source}: vms must be a non-empty list")
    vms: list[VMDefinition] = []
    vm_fields = {
        "name",
        "cpus",
        "ram",
        "disk",
        "image",
        "hostname",
        "interfaces",
        "setup",
        "depends_on",
        "ssh_user",
    }
    required_vm = vm_fields - {"ssh_user"}
    for index, item in enumerate(data["vms"]):
        prefix = f"vms.{index}"
        vm = _mapping(item, prefix, vm_fields, required_vm)
        if not isinstance(vm["name"], str) or not VM_NAME.fullmatch(vm["name"]):
            raise DefinitionError(f"{prefix}.name: invalid DNS-safe VM name")
        if not isinstance(vm["cpus"], int) or isinstance(vm["cpus"], bool) or vm["cpus"] <= 0:
            raise DefinitionError(f"{prefix}.cpus: must be positive")
        parse_size(vm["ram"])
        parse_size(vm["disk"])
        interfaces = vm["interfaces"]
        if not isinstance(interfaces, list) or len(interfaces) != 1:
            raise DefinitionError(f"{prefix}.interfaces: schema v1 requires exactly one interface")
        interface = _mapping(interfaces[0], f"{prefix}.interfaces.0", {"type"}, {"type"})
        if interface["type"] != "isolated_nat":
            raise DefinitionError(f"{prefix}.interfaces.0.type: must be isolated_nat")
        dependencies = vm["depends_on"]
        if not isinstance(dependencies, list) or not all(
            isinstance(dep, str) for dep in dependencies
        ):
            raise DefinitionError(f"{prefix}.depends_on: must be a string list")
        if vm["name"] in dependencies:
            raise DefinitionError(f"{prefix}.depends_on: self-dependency")
        for field in ("image", "hostname"):
            if not isinstance(vm[field], str) or not vm[field]:
                raise DefinitionError(f"{prefix}.{field}: must be a non-empty string")
        ssh_user = vm.get("ssh_user")
        if ssh_user is not None and (not isinstance(ssh_user, str) or not ssh_user):
            raise DefinitionError(f"{prefix}.ssh_user: must be a non-empty string")
        vms.append(
            VMDefinition(
                vm["name"],
                vm["cpus"],
                vm["ram"],
                vm["disk"],
                vm["image"],
                vm["hostname"],
                (Interface("isolated_nat"),),
                _script(source.parent, vm["setup"], f"{prefix}.setup"),
                tuple(dependencies),
                ssh_user,
            )
        )
    names = [vm.name for vm in vms]
    if len(names) != len(set(names)):
        raise DefinitionError(f"{source}: duplicate VM name")
    for index, defined_vm in enumerate(vms):
        for dependency in defined_vm.depends_on:
            if dependency not in names:
                raise DefinitionError(f"vms.{index}.depends_on: unknown dependency {dependency}")
    grading_data = data.get("grading")
    reset_vms: tuple[str, ...] = ()
    if "grading" in data:
        grading = _mapping(grading_data, "grading", {"reset_vms"}, {"reset_vms"})
        selected = grading["reset_vms"]
        if not isinstance(selected, list) or not all(isinstance(name, str) for name in selected):
            raise DefinitionError("grading.reset_vms: must be a string list")
        if not selected:
            raise DefinitionError("grading.reset_vms: must be non-empty")
        if len(selected) != len(set(selected)):
            raise DefinitionError("grading.reset_vms: duplicate VM")
        unknown = next((name for name in selected if name not in names), None)
        if unknown is not None:
            raise DefinitionError(f"grading.reset_vms: unknown VM {unknown}")
        reset_vms = tuple(selected)
    definition = LabDefinition(
        schema_version=1,
        id=data["id"],
        title=data["title"],
        goal=data["goal"],
        instructions=data["instructions"],
        provider=data["provider"],
        grader=_script(source.parent, data["grader"], "grader"),
        vms=tuple(vms),
        source=source,
        grading=GradingDefinition(reset_vms),
        external=external,
    )
    definition.dependency_order()
    return definition


def directory_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            kind, content = b"symlink", os.readlink(path).encode()
        elif path.is_dir():
            kind, content = b"directory", b""
        elif path.is_file():
            kind, content = b"file", path.read_bytes()
        else:
            raise DefinitionError(f"unsupported filesystem entry in definition: {path}")
        digest.update(len(relative).to_bytes(8, "big") + relative + kind)
        digest.update(len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def discover(
    builtins: list[Path], external: list[Path], *, allow_override: bool = False
) -> dict[str, LabDefinition]:
    found: dict[str, LabDefinition] = {}
    for directory in builtins:
        definition = load_definition(directory / "lab.yaml")
        if definition.id in found:
            raise DefinitionError(f"duplicate bundled definition {definition.id}")
        found[definition.id] = definition
    external_ids: set[str] = set()
    expanded = [
        child
        for location in external
        for child in (
            [location]
            if (location / "lab.yaml").is_file()
            else sorted(path.parent for path in location.glob("*/lab.yaml"))
        )
    ]
    for directory in expanded:
        definition = load_definition(directory / "lab.yaml", external=True)
        if definition.id in external_ids:
            raise DefinitionError(f"duplicate external definition {definition.id}")
        external_ids.add(definition.id)
        if definition.id in found and not allow_override:
            raise DefinitionError(f"external definition {definition.id} requires explicit override")
        found[definition.id] = definition
    return found
