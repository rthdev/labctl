"""Bounded, read-only runtime snapshots, separate from lifecycle reconciliation."""

from __future__ import annotations

import copy
import json
import os
import re
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException

from labctl.state import StateError, load_state


def _run_command(
    argv: list[str], *, timeout: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Bound the query and its transport helpers without logging URI credentials."""
    with subprocess.Popen(  # noqa: S603 - explicit read-only argument vector
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            # Killing only virsh can leave a remote SSH transport behind.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


_POWER = {
    "1": "running",
    "2": "blocked",
    "3": "paused",
    "4": "shutdown",
    "5": "stopped",
    "6": "crashed",
    "7": "pmsuspended",
}


class ObservationError(Exception):
    """Only fixed, secret-free diagnostics cross this boundary."""


def status_records(root: Path, lab_id: str | None = None) -> dict[str, dict[str, Any]]:
    """Read state and allowlisted journal summaries without migration or recovery."""
    states = {}
    for path in sorted((root / "labs").glob("*.json")):
        if lab_id is not None and path.stem != lab_id:
            continue
        state = load_state(path, migrate=False)
        if state["id"] != path.stem:
            raise StateError("saved lab identity does not match its filename")
        states[path.stem] = state
    for path in sorted((root / "transactions").glob("*.json")):
        if lab_id is not None and path.stem != lab_id:
            continue
        state = states.setdefault(path.stem, {"id": path.stem, "state": None, "vms": {}})
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(document, dict)
                or document.get("schema_version") != 1
                or document.get("id") != path.stem
            ):
                raise ValueError
            operation = document.get("operation")
            phase = document.get("phase")
            state["pending_operation"] = {
                "operation": operation
                if operation in ("create", "reset", "remove", "remove-vm")
                else "unknown",
                "phase": phase if phase in ("prepared", "rollback", "committed") else None,
            }
            selected = document.get("selected_vms")
            if isinstance(selected, list) and all(
                isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9-]*", name)
                for name in selected
            ):
                state["pending_operation"]["selected_vms"] = selected
        except (OSError, ValueError):
            state["pending_operation"] = {"operation": "unknown", "phase": None}
    return states


def status_diagnostics(states: dict[str, dict[str, Any]]) -> tuple[str, ...]:
    messages: list[str] = []
    for lab_id, state in states.items():
        messages.extend(f"{lab_id}: {message}" for message in state["observation"]["diagnostics"])
        if state.get("pending_operation"):
            messages.append(
                f"{lab_id}: pending {state['pending_operation']['operation']} transaction; "
                "inspect the lab and use explicit lifecycle recovery."
            )
        if state.get("recorded_state") in {"provisioning", "degraded", "failed_cleanup"}:
            messages.append(
                f"{lab_id}: recorded lifecycle is {state['recorded_state']}; "
                "inspect saved failure/provisioning details."
            )
    return tuple(messages)


def _snapshot(output: str) -> dict[str, str]:
    domains: dict[str, str] = {}
    for block in re.split(r"\n\s*\n", output.strip()):
        if not block:
            continue
        match = re.fullmatch(
            r"Domain: '([^\n]+)'\n\s+state\.state=(\d+)\n\s+state\.reason=\d+\s*", block
        )
        if not match or match[1] in domains:
            raise ObservationError("Invalid provider response; check virsh/libvirt compatibility.")
        domains[match[1]] = match[2]
    return domains


def observe_states(states: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Observe recorded KVM endpoints only; never discover or select a fallback URI.

    API-v1 external providers have no runtime observation contract. Do not invoke
    their reconcile/doctor methods or accidentally substitute the built-in KVM.
    """
    result = copy.deepcopy(states)
    deadline = time.monotonic() + 10
    snapshots: dict[str, dict[str, str] | ObservationError] = {}

    def query(uri: str, *args: str) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ObservationError("Observation budget exhausted; retry a narrower VM/lab listing.")
        try:
            completed = _run_command(
                ["virsh", "--readonly", "--connect", uri, *args],
                env={**os.environ, "LC_ALL": "C"},
                timeout=min(3, remaining),
            )
        except subprocess.TimeoutExpired:
            raise ObservationError(
                "Provider query timed out; check recorded URI connectivity."
            ) from None
        except OSError:
            raise ObservationError(
                "Cannot execute virsh; check installation and executable access."
            ) from None
        except UnicodeError:
            raise ObservationError(
                "Invalid provider text; check virsh/libvirt compatibility."
            ) from None
        if completed.returncode:
            raise ObservationError(
                "Provider query failed; check recorded URI, libvirt service and read authorization."
            )
        return completed.stdout

    for state in result.values():
        uri = state.get("provider_uri")
        uid = state.get("instance_uid")
        for name, vm in state.get("vms", {}).items():
            observed = "unknown"
            diagnostics: list[str] = []
            power: str | None = None
            try:
                if state.get("provider") != "kvm":
                    raise ObservationError(
                        "Runtime observation unsupported or provider identity missing; "
                        "inspect recorded provider (API v1)."
                    )
                domain = vm.get("domain")
                if (
                    not isinstance(uri, str)
                    or not uri
                    or not isinstance(uid, str)
                    or not uid
                    or not isinstance(domain, str)
                    or not domain
                ):
                    raise ObservationError(
                        "Recorded URI or ownership identity missing; inspect saved state."
                    )
                if (
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", uid)
                    or not re.fullmatch(r"[a-z][a-z0-9-]*", name)
                    or domain != f"labctl-{uid}-{name}"
                ):
                    raise ObservationError(
                        "Recorded domain identity is invalid; inspect saved state."
                    )
                if uri not in snapshots:
                    try:
                        snapshots[uri] = _snapshot(query(uri, "domstats", "--state"))
                    except ObservationError as exc:
                        snapshots[uri] = exc
                snapshot = snapshots[uri]
                if isinstance(snapshot, ObservationError):
                    raise snapshot
                if domain not in snapshot:
                    observed = "missing"
                    diagnostics.append(
                        "Domain absent from recorded provider; inspect before recovery."
                    )
                else:
                    try:
                        xml = SafeElementTree.fromstring(query(uri, "dumpxml", domain))
                    except (ElementTree.ParseError, DefusedXmlException):
                        raise ObservationError(
                            "Invalid domain XML; inspect provider configuration."
                        ) from None
                    owner = xml.find("metadata/{urn:labctl}ownership")
                    expected = {
                        f"{{urn:labctl}}{key}": value
                        for key, value in {
                            "provider": "kvm",
                            "uid": uid,
                            "resource-type": "domain",
                            "vm-name": name,
                        }.items()
                    }
                    if (
                        xml.findtext("name") != domain
                        or owner is None
                        or len(owner) != len(expected)
                        or {child.tag: child.text for child in owner} != expected
                    ):
                        raise ObservationError(
                            "Ownership mismatch; inspect saved identity and domain metadata."
                        )
                    power = snapshot[domain]
                    observed = _POWER.get(power, "unknown")
                    if observed == "unknown":
                        diagnostics.append("Unrecognized provider power state; inspect libvirt.")
            except ObservationError as exc:
                diagnostics.append(str(exc))
            vm.update(
                recorded_state=vm.get("state", "unknown"),
                state=observed,
                observation={
                    "state": observed,
                    "provider_state": power,
                    "diagnostics": diagnostics,
                },
            )
        aggregate = _aggregate(state.get("vms", {}))
        state.update(
            recorded_state=state.get("state", "unknown"),
            state=aggregate,
            observation={
                "state": aggregate,
                "diagnostics": [
                    f"{name}: {diagnostic}"
                    for name, vm in state.get("vms", {}).items()
                    for diagnostic in vm["observation"]["diagnostics"]
                ],
            },
        )
        if not state.get("vms"):
            state["observation"]["diagnostics"].append(
                "No recorded VM identities; inspect lifecycle state or pending transaction."
            )
    return result


def _aggregate(vms: dict[str, Any]) -> str:
    powers = {vm["state"] for vm in vms.values()}
    if not powers or "unknown" in powers:
        return "unknown"
    if powers == {"running"}:
        return "running"
    if powers == {"stopped"}:
        return "stopped"
    if powers == {"running", "stopped"}:
        return "partial"
    return "degraded"
