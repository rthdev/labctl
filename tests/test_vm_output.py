import json
from io import StringIO

import pytest

from labctl.application import CommandResult
from labctl.cli import main


class Application:
    def execute(self, args):
        return CommandResult("vm", {"id": "LX001", "vms": {"node": {"state": "ready"}}})


@pytest.mark.parametrize(("command", "verb"), [("start", "started"), ("stop", "stopped")])
def test_vm_power_default_output_is_concise(command, verb):
    out, err = StringIO(), StringIO()
    assert (
        main(["vm", command, "LX001", "node"], application=Application(), stdout=out, stderr=err)
        == 0
    )
    assert out.getvalue() == f"VM LX001/node {verb}.\n"
    assert err.getvalue() == ""


@pytest.mark.parametrize("command", ["start", "stop"])
@pytest.mark.parametrize("flag", ["-v", "--verbose", "--debug", "--json", "--quiet"])
def test_vm_power_preserves_explicit_output_modes(command, flag):
    out, err = StringIO(), StringIO()
    assert (
        main(
            ["vm", command, "LX001", "node", flag],
            application=Application(),
            stdout=out,
            stderr=err,
        )
        == 0
    )
    if flag == "--quiet":
        assert out.getvalue() == ""
    elif flag == "--json":
        assert json.loads(out.getvalue()) == {
            "schema_version": 1,
            "ok": True,
            "kind": "vm",
            "data": {"id": "LX001", "vms": {"node": {"state": "ready"}}},
        }
    else:
        assert out.getvalue() == "id: LX001\nvms: {'node': {'state': 'ready'}}\n"
    assert err.getvalue() == ""


@pytest.mark.parametrize("command", ["start", "stop"])
def test_vm_power_failure_does_not_claim_success(command):
    class FailedApplication:
        def execute(self, args):
            return CommandResult("vm", {"reason": "operation failed"}, status=6)

    out, err = StringIO(), StringIO()
    assert (
        main(
            ["vm", command, "LX001", "node"],
            application=FailedApplication(),
            stdout=out,
            stderr=err,
        )
        == 6
    )
    assert out.getvalue() == "reason: operation failed\n"
    assert err.getvalue() == "error: vm command failed\n"
