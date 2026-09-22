from __future__ import annotations

import pytest
import yaml

from labctl.cloudinit import ReadinessPhase, ReadinessTimeout, generate_cloud_init, wait_ready


def test_cloud_init_disables_password_ssh_and_runs_setup_once() -> None:
    rendered = generate_cloud_init("ssh-ed25519 AAAA lab", ["dnf -y install git", "touch /ready"])
    assert rendered.startswith("#cloud-config\n")
    document = yaml.safe_load(rendered)
    assert document["ssh_pwauth"] is False
    assert document["disable_root"] is True
    assert document["users"][0]["ssh_authorized_keys"] == ["ssh-ed25519 AAAA lab"]
    script = document["write_files"][0]["content"]
    assert "test -e /var/lib/labctl/setup.done && exit 0" in script
    assert script.index("dnf -y install git") < script.index("touch /ready")
    assert script.rstrip().endswith("touch /var/lib/labctl/setup.done")
    assert document["runcmd"] == [["/usr/local/sbin/labctl-setup"]]


def test_readiness_uses_phase_specific_timeouts() -> None:
    now = 0.0
    checks: list[str] = []

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    phases = [
        ReadinessPhase("address", 2, lambda: checks.append("address") or len(checks) >= 2),
        ReadinessPhase("ssh", 1, lambda: True),
    ]
    wait_ready(phases, interval=0.5, monotonic=monotonic, sleep=sleep)
    assert checks == ["address", "address"]

    with pytest.raises(ReadinessTimeout, match="cloud-init"):
        wait_ready(
            [ReadinessPhase("cloud-init", 1, lambda: False)],
            interval=0.5,
            monotonic=monotonic,
            sleep=sleep,
        )
