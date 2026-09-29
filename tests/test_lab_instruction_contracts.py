"""Canonical learner contracts live in definitions, never setup scripts."""

from pathlib import Path

import pytest

from labctl.definitions import load_definition

LABS = Path(__file__).parents[1] / "src/labctl/data/labs"
DEFINITIONS = sorted(LABS.glob("*/lab.yaml"))
REQUIRED = {
    "LX001": [
        "labops",
        "opsadmin",
        "supplementary",
        "/srv/labshare/operations.txt",
        "2770",
        "0660",
    ],
    "LX002": [
        "/srv/lx002/source/records.txt",
        "/srv/lx002/archive/records.hard",
        "/home/student/current-records",
        "ERROR 2",
        "INFO 2",
        "WARN 2",
        "0640",
    ],
    "LX101": [
        "http://127.0.0.1/health",
        "LX101 healthy",
        "/etc/systemd/journald.conf.d/lx101.conf",
        "Storage=persistent",
        "/var/log/journal",
    ],
    "LX102": [
        "/var/lib/labctl-media/lx102-storage.img /srv/archive xfs loop,nofail 0 0",
        "/srv/archive/persistent.txt",
        "LX102 persistent storage",
        "findmnt --verify",
    ],
    "LX201": [
        "NetworkManager",
        "UUID",
        "runtime",
        "permanent",
        "cockpit",
        "http://127.0.0.1/network-health",
        "LX201 reachable",
    ],
    "LX202": [
        "/usr/local/sbin/lx202-maintenance",
        "lx202-maintenance.service",
        "lx202-maintenance.timer",
        "/var/tmp/lx202-cache/stale.tmp",  # noqa: S108 - required guest path
        "Type=oneshot",
        "OnCalendar=02:15",
        "Persistent=true",
        "/var/log/lx202-maintenance.log",
        "maintenance complete",
    ],
    "LX301": [
        "/srv/secureweb/index.html",
        "http_port_t",
        "httpd_sys_content_t",
        "httpd_t",
        "/etc/selinux/config",
        "http://127.0.0.1:8088/",
        "LX301 secure service",
    ],
    "LX302": [
        "/var/lib/labctl-media/lx302-pv1.img",
        "/var/lib/labctl-media/lx302-pv2.img",
        "700 MiB",
        "650 MiB",
        "DefaultDependencies=no",
        "local-fs-pre.target",
        "lvm2-monitor.service",
        "WantedBy=sysinit.target",
        "pvscan",
        "/dev/vgdata/lvdata",
        "/srv/lvmdata/recovery.txt",
        "LX302 recovered and expanded",
    ],
    "LX401": [
        "audit=O",
        "audit=0",
        "/etc/default/grub",
        "/boot/loader/entries/*.conf",
        "/etc/selinux/config",
        "/.autorelabel",
        "/etc/shadow",
        "multi-user.target",
        "/var/lib/labctl-media/lx401-recovery.img /srv/recovery xfs loop,nofail 0 0",
    ],
    "LX402": [
        "/var/lib/labctl-media/lx402-incident.img",
        "ext4",
        "/srv/incident/runaway.log",
        "below 70%",
        "http://127.0.0.1:8090/health",
        "lx402-burner",
        "lx402-api",
        "root cause: disk pressure and runaway burner",
        "recovery: capacity restored; api enabled and healthy",
    ],
    "AN202": ["firewalld", "enabled and active", "runtime and permanently"],
    "AN302": ["no supplementary groups"],
}


def test_complete_catalogue() -> None:
    assert len(DEFINITIONS) == 30
    assert {
        family: sum(p.parent.name.startswith(family) for p in DEFINITIONS)
        for family in ("AN", "CT", "LX")
    } == {"AN": 10, "CT": 10, "LX": 10}


@pytest.mark.parametrize("path", DEFINITIONS, ids=lambda p: p.parent.name)
def test_setup_has_no_assignment_document_writer(path: Path) -> None:
    for setup in path.parent.glob("*.sh"):
        assert "LAB.md" not in setup.read_text(), setup


@pytest.mark.parametrize("path", DEFINITIONS, ids=lambda p: p.parent.name)
def test_self_contained_canonical_assignment(path: Path) -> None:
    definition = load_definition(path)
    body = definition.instructions
    assert not body.startswith("# "), "The shared renderer supplies the title"
    assert "LAB.md" not in body, "Do not refer learners back to this same document"
    assert "host" in body
    assert f"grade {definition.id}" in body
    for required in REQUIRED.get(definition.id, []):
        assert required in body, f"{definition.id}: missing {required}"
    if definition.id.startswith("AN"):
        for required in (
            "ansible all -i inventory.ini -m ping",
            "ansible-playbook -i inventory.ini site.yml",
            "current-state",
            "clean-baseline",
            "[y/N]",
            "--yes",
        ):
            assert required in body
        for target in definition.grading.reset_vms:
            assert f"ssh {target}" in body
    if definition.id.startswith("CT"):
        for required in ("rootless", "--pull=never", "mutable", "Internet"):
            assert required in body
