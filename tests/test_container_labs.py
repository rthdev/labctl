# ruff: noqa: E501
"""Contracts for the progressive rootless Podman curriculum."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from labctl.definitions import load_definition

ROOT = Path(__file__).parents[1]
LABS = ROOT / "src/labctl/data/labs"


def _fake_ssh(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "ssh.jsonl"
    executable = tmp_path / "ssh"
    executable.write_text(
        """#!/usr/bin/python3
import json, os, sys
with open(os.environ['FAKE_SSH_LOG'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
answers = json.loads(os.environ['FAKE_SSH_ANSWERS'])
answer = answers.get(sys.argv[-1])
if answer is None:
    raise SystemExit(255)
if isinstance(answer, dict):
    print(answer.get('stdout', ''))
    raise SystemExit(answer.get('returncode', 0))
print(answer)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable, log


def _run(
    tmp_path: Path, lab_id: str, answers: dict[str, object]
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    ssh, log = _fake_ssh(tmp_path)
    return _run_with_ssh(tmp_path, lab_id, ssh, log=log, answers=answers)


def _run_with_ssh(
    tmp_path: Path,
    lab_id: str,
    ssh: Path,
    *,
    log: Path | None = None,
    answers: dict[str, object] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    definition = load_definition(LABS / lab_id / "lab.yaml")
    identity = tmp_path / "id"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("fixture\n", encoding="utf-8")
    known_hosts.write_text("192.0.2.10 ssh-ed25519 fixture\n", encoding="utf-8")
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "lab_id": lab_id,
                "hosts": {
                    "node": {
                        "address": "192.0.2.10",
                        "ssh_user": "student",
                        "private_key_path": str(identity),
                        "known_hosts_path": str(known_hosts),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [definition.grader, context],
        env={
            "LABCTL_SSH": str(ssh),
            "FAKE_SSH_LOG": str(log or tmp_path / "unused-log"),
            "FAKE_SSH_ANSWERS": json.dumps(answers or {}),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    calls = (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log is not None and log.exists()
        else []
    )
    return result, calls


def _assert_lab(tmp_path: Path, lab_id: str, title: str, answers: dict[str, object]) -> None:
    definition = load_definition(LABS / lab_id / "lab.yaml")
    assert definition.title == title
    assert [vm.name for vm in definition.vms] == ["node"]
    assert definition.vms[0].image == "rocky:9"
    assert definition.vms[0].ssh_user == "student"
    assert os.access(definition.grader, os.X_OK)
    assert os.access(definition.vms[0].setup, os.X_OK)
    assert len(definition.instructions.splitlines()) >= 6
    setup = definition.vms[0].setup.read_text(encoding="utf-8")
    assert "/home/student/LAB.md" in setup
    assert "loginctl enable-linger student" in setup
    assert "XDG_RUNTIME_DIR" in setup
    assert "podman pull docker.io/library/alpine:3.20" in setup
    assert "podman run --privileged" not in setup
    subprocess.run(["/usr/bin/bash", "-n", definition.vms[0].setup], check=True)

    passed, calls = _run(tmp_path, lab_id, answers)
    assert passed.returncode == 0, passed.stderr
    assert all(line.startswith(f"PASS {lab_id}: ") for line in passed.stdout.splitlines())
    assert calls
    assert all("StrictHostKeyChecking=yes" in call for call in calls)
    assert all("ConnectTimeout=10" in call for call in calls)
    assert all("UserKnownHostsFile=" + str(tmp_path / "known_hosts") in call for call in calls)
    assert any(call[-1].startswith("/usr/bin/podman inspect ") for call in calls)

    first = next(command for command in answers if command.startswith("/usr/bin/podman inspect "))
    failed_answers = dict(answers)
    failed_answers[first] = {"stdout": "{}", "returncode": 0}
    failed, _calls = _run(tmp_path / "negative", lab_id, failed_answers)
    assert failed.returncode == 1
    assert f"FAIL {lab_id}:" in failed.stdout


ROOTLESS_INFO = json.dumps(
    {
        "host": {"security": {"rootless": True, "selinuxEnabled": True}},
        "store": {"graphRoot": "/home/student/.local/share/containers/storage"},
    }
)
CT_IDS = ("CT001", "CT002", "CT101", "CT102", "CT201", "CT202", "CT301", "CT302", "CT401", "CT402")


def test_beginner_guides_include_safe_exact_commands() -> None:
    ct001 = (LABS / "CT001/setup.sh").read_text(encoding="utf-8")
    ct002 = (LABS / "CT002/setup.sh").read_text(encoding="utf-8")

    assert "podman run --name ct001-hello" in ct001
    assert "--rm" in ct001 and "Do not use" in ct001
    assert "podman ps --all" in ct001
    assert "podman logs ct001-hello" in ct001
    assert "podman run --name ct002-worker" in ct002
    assert "podman stop ct002-worker" in ct002
    assert "podman start ct002-worker" in ct002
    assert "podman exec ct002-worker" in ct002


@pytest.mark.parametrize("lab_id", CT_IDS)
def test_container_graders_report_unexecutable_ssh_without_traceback(
    tmp_path: Path, lab_id: str
) -> None:
    ssh = tmp_path / "ssh"
    ssh.write_text("not executable\n", encoding="utf-8")
    result, _calls = _run_with_ssh(tmp_path, lab_id, ssh)

    assert result.returncode == 2
    assert f"ERROR {lab_id}:" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("lab_id", CT_IDS)
def test_container_graders_fail_closed_on_malformed_context(tmp_path: Path, lab_id: str) -> None:
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"schema_version": 1, "lab_id": lab_id, "hosts": {"node": []}}))
    grader = load_definition(LABS / lab_id / "lab.yaml").grader

    result = subprocess.run(
        [grader, context],
        env={"LABCTL_SSH": "/usr/bin/ssh"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert f"ERROR {lab_id}:" in result.stderr
    source = grader.read_text(encoding="utf-8")
    assert "timeout=15" in source
    assert "ConnectTimeout=10" in source
    assert "StrictHostKeyChecking=yes" in source


def test_ct001_one_shot_container(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "CT001",
        "Run your first rootless container",
        {
            "/usr/bin/podman info --format json": ROOTLESS_INFO,
            "/usr/bin/podman inspect ct001-hello": json.dumps(
                [
                    {
                        "Name": "ct001-hello",
                        "ProcessLabel": "system_u:system_r:container_t:s0:c1,c2",
                        "State": {"Status": "exited", "ExitCode": 0},
                        "Config": {
                            "Image": "docker.io/library/alpine:3.20",
                            "Cmd": ["printf", "CT001 container complete\\n"],
                        },
                        "HostConfig": {"Privileged": False},
                    }
                ]
            ),
            "/usr/bin/podman logs ct001-hello": "CT001 container complete",
        },
    )


def test_ct002_lifecycle_logs_and_exec(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "CT002",
        "Manage container lifecycle and state",
        {
            "/usr/bin/podman info --format json": ROOTLESS_INFO,
            "/usr/bin/podman inspect ct002-worker": json.dumps(
                [
                    {
                        "Name": "ct002-worker",
                        "ProcessLabel": "system_u:system_r:container_t:s0:c1,c2",
                        "State": {"Status": "running"},
                        "Config": {
                            "Image": "docker.io/library/alpine:3.20",
                            "Cmd": [
                                "sh",
                                "-c",
                                "echo 'CT002 worker ready'; exec sleep infinity",
                            ],
                        },
                        "HostConfig": {"Privileged": False},
                    }
                ]
            ),
            "/usr/bin/podman exec ct002-worker cat /lesson/status": "lifecycle inspected",
            "/usr/bin/podman logs ct002-worker": "CT002 worker ready",
        },
    )


def test_ct101_web_port(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "CT101",
        "Publish a rootless web service",
        {
            "/usr/bin/podman info --format json": ROOTLESS_INFO,
            "/usr/bin/podman inspect ct101-web": json.dumps(
                [
                    {
                        "Name": "ct101-web",
                        "ProcessLabel": "system_u:system_r:container_t:s0:c1,c2",
                        "State": {"Status": "running"},
                        "Config": {"Image": "docker.io/library/alpine:3.20"},
                        "HostConfig": {
                            "Privileged": False,
                            "PortBindings": {
                                "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]
                            },
                        },
                    }
                ]
            ),
            "/usr/bin/podman exec ct101-web wget -qO- http://127.0.0.1:8080/index.html": "CT101 rootless web",
            "/usr/bin/curl --noproxy '*' --max-time 5 -fsS http://127.0.0.1:8080/index.html": "CT101 rootless web",
        },
    )


def test_ct102_named_volume(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "CT102",
        "Persist data in a named volume",
        {
            "/usr/bin/podman info --format json": ROOTLESS_INFO,
            "/usr/bin/podman inspect ct102-writer": json.dumps(
                [
                    {
                        "Name": "ct102-writer",
                        "ProcessLabel": "system_u:system_r:container_t:s0:c1,c2",
                        "State": {"Status": "running"},
                        "Config": {"Image": "docker.io/library/alpine:3.20"},
                        "HostConfig": {"Privileged": False},
                        "Mounts": [
                            {
                                "Type": "volume",
                                "Name": "ct102-data",
                                "Destination": "/data",
                                "Options": ["nosuid", "nodev", "rbind"],
                            }
                        ],
                    }
                ]
            ),
            "/usr/bin/podman exec ct102-writer cat /data/message.txt": "CT102 durable data",
            "/usr/bin/podman volume inspect ct102-data": json.dumps(
                [
                    {
                        "Name": "ct102-data",
                        "Mountpoint": "/home/student/.local/share/containers/storage/volumes/ct102-data/_data",
                    }
                ]
            ),
        },
    )


def test_ct201_containerfile_build(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "CT201",
        "Build an image with a Containerfile",
        {
            "/usr/bin/podman info --format json": ROOTLESS_INFO,
            "/usr/bin/podman inspect ct201-app": json.dumps(
                [
                    {
                        "Name": "ct201-app",
                        "Image": "a" * 64,
                        "Mounts": [],
                        "ProcessLabel": "system_u:system_r:container_t:s0:c1,c2",
                        "State": {"Status": "running"},
                        "Config": {
                            "Image": "localhost/ct201-app:1",
                            "Labels": {"org.opencontainers.image.title": "CT201 app"},
                        },
                        "HostConfig": {"Privileged": False},
                    }
                ]
            ),
            "/usr/bin/podman image inspect localhost/ct201-app:1": json.dumps(
                [
                    {
                        "RepoTags": ["localhost/ct201-app:1"],
                        "Id": "a" * 64,
                        "Labels": {"org.opencontainers.image.title": "CT201 app"},
                    }
                ]
            ),
            "/usr/bin/podman exec ct201-app cat /app/result.txt": "CT201 image build",
            "/usr/bin/podman diff ct201-app": "",
        },
    )


def test_ct202_private_network(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "CT202",
        "Connect services on a private network",
        {
            "/usr/bin/podman info --format json": ROOTLESS_INFO,
            "/usr/bin/podman inspect ct202-api ct202-client": json.dumps(
                [
                    {
                        "Name": "ct202-api",
                        "ProcessLabel": "system_u:system_r:container_t:s0:c1,c2",
                        "State": {"Status": "running"},
                        "Config": {"Image": "docker.io/library/alpine:3.20"},
                        "HostConfig": {"Privileged": False},
                        "NetworkSettings": {
                            "Networks": {
                                "ct202-private": {"Aliases": ["api"], "NetworkID": "n" * 64}
                            }
                        },
                    },
                    {
                        "Name": "ct202-client",
                        "ProcessLabel": "system_u:system_r:container_t:s0:c1,c2",
                        "State": {"Status": "running"},
                        "Config": {"Image": "docker.io/library/alpine:3.20"},
                        "HostConfig": {"Privileged": False},
                        "NetworkSettings": {"Networks": {"ct202-private": {"NetworkID": "n" * 64}}},
                    },
                ]
            ),
            "/usr/bin/podman network inspect ct202-private": json.dumps(
                [{"name": "ct202-private", "internal": True, "id": "n" * 64}]
            ),
            "/usr/bin/podman exec ct202-client wget -qO- http://api:8080/health": "CT202 private API",
        },
    )


def test_ct301_pod_and_config(tmp_path: Path) -> None:
    from test_container_review import module

    _assert_lab(
        tmp_path,
        "CT301",
        "Run a configured application pod",
        {
            "/usr/bin/podman info --format json": ROOTLESS_INFO,
            "/usr/bin/podman pod inspect ct301-stack": json.dumps(
                [
                    {
                        "Name": "ct301-stack",
                        "Id": "p" * 64,
                        "State": "Running",
                        "InfraContainerID": "i" * 64,
                        "Containers": [
                            {"Name": "ct301-web", "Id": "w" * 64},
                            {"Name": "ct301-checker", "Id": "c" * 64},
                            {"Name": "infra", "Id": "i" * 64},
                        ],
                    }
                ]
            ),
            "/usr/bin/podman inspect ct301-web ct301-checker": json.dumps(
                [
                    {
                        "Name": "ct301-web",
                        "Id": "w" * 64,
                        "Pod": "p" * 64,
                        "ProcessLabel": "system_u:system_r:container_t:s0:c1,c2",
                        "State": {"Status": "running"},
                        "Config": {
                            "Image": "docker.io/library/alpine:3.20",
                            "Env": ["APP_MODE=training"],
                        },
                        "HostConfig": {"Privileged": False},
                        "Mounts": [
                            {
                                "Type": "bind",
                                "Source": "/home/student/ct301/app.conf",
                                "Destination": "/etc/ct301/app.conf",
                                "Options": ["rbind"],
                                "RW": False,
                            }
                        ],
                    },
                    {
                        "Name": "ct301-checker",
                        "Id": "c" * 64,
                        "Pod": "p" * 64,
                        "ProcessLabel": "system_u:system_r:container_t:s0:c1,c2",
                        "State": {"Status": "running"},
                        "Config": {"Image": "docker.io/library/alpine:3.20"},
                        "HostConfig": {"Privileged": False},
                    },
                ]
            ),
            "/usr/bin/podman exec ct301-checker wget -qO- http://127.0.0.1:8080/config": "mode=training",
            "/usr/bin/podman exec ct301-web cat /etc/ct301/app.conf": "mode=training",
            module("CT301").SERVICE_COMMAND: "connected",
        },
    )


def test_ct302_effective_hardening(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "CT302",
        "Harden and limit a rootless workload",
        {
            "/usr/bin/podman info --format json": ROOTLESS_INFO,
            "/usr/bin/podman inspect ct302-secure": json.dumps(
                [
                    {
                        "Name": "ct302-secure",
                        "ProcessLabel": "system_u:system_r:container_t:s0:c1,c2",
                        "State": {"Status": "running"},
                        "Config": {"Image": "docker.io/library/alpine:3.20", "User": "10001:10001"},
                        "HostConfig": {
                            "Privileged": False,
                            "ReadonlyRootfs": True,
                            "Memory": 268435456,
                            "NanoCpus": 500000000,
                            "PidsLimit": 64,
                            "CapDrop": ["ALL"],
                            "SecurityOpt": ["no-new-privileges"],
                        },
                        "EffectiveCaps": [],
                        "BoundingCaps": [],
                    }
                ]
            ),
            '/usr/bin/podman exec ct302-secure sh -ec \'id -u; id -g; awk \'"\'"\'$2 == "/" {n=split($4,a,","); for(i=1;i<=n;i++) if(a[i]=="ro") print "ro"}\'"\'"\' /proc/mounts; grep -E \'"\'"\'^(NoNewPrivs|CapEff|CapBnd):\'"\'"\' /proc/1/status; cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/pids.max /sys/fs/cgroup/cpu.max\'': "10001\n10001\nro\nNoNewPrivs:\t1\nCapEff:\t0000000000000000\nCapBnd:\t0000000000000000\n268435456\n64\n50000 100000",
        },
    )


def test_ct401_quadlet_reboot_persistence(tmp_path: Path) -> None:
    _assert_lab(
        tmp_path,
        "CT401",
        "Persist a rootless service with Quadlet",
        {
            "/usr/bin/podman info --format json": ROOTLESS_INFO,
            "/usr/bin/podman inspect ct401-web": json.dumps(
                [
                    {
                        "Name": "ct401-web",
                        "ProcessLabel": "system_u:system_r:container_t:s0:c1,c2",
                        "State": {"Status": "running", "ConmonPid": 1234},
                        "Config": {"Image": "docker.io/library/alpine:3.20"},
                        "HostConfig": {"Privileged": False, "RestartPolicy": {"Name": "no"}},
                    }
                ]
            ),
            "/usr/bin/cat /var/lib/labctl/ct401-boot-baseline /proc/sys/kernel/random/boot_id": "11111111-1111-4111-8111-111111111111\n22222222-2222-4222-8222-222222222222",
            "/usr/bin/loginctl show-user student -p Linger --value": "yes",
            "/usr/bin/systemctl --user show ct401-web.service -p ActiveState -p MainPID -p SourcePath -p FragmentPath": "ActiveState=active\nMainPID=1234\nSourcePath=/home/student/.config/containers/systemd/ct401-web.container\nFragmentPath=/run/user/1000/systemd/generator/ct401-web.service",
            "/usr/bin/systemctl --user show default.target -p Wants --value": "ct401-web.service",
            "/usr/bin/podman exec ct401-web cat /srv/status.txt": "CT401 persistent service",
        },
    )


def test_ct402_incident_recovery(tmp_path: Path) -> None:
    from test_container_review import module

    _assert_lab(
        tmp_path,
        "CT402",
        "Recover a rootless container incident",
        {
            "/usr/bin/podman info --format json": ROOTLESS_INFO,
            "/usr/bin/podman inspect ct402-api": json.dumps(
                [
                    {
                        "Name": "ct402-api",
                        "ProcessLabel": "system_u:system_r:container_t:s0:c1,c2",
                        "State": {"Status": "running"},
                        "Config": {
                            "Image": "docker.io/library/alpine:3.20",
                            "Env": ["API_PORT=8080"],
                        },
                        "HostConfig": {
                            "Privileged": False,
                            "PortBindings": {
                                "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]
                            },
                        },
                        "Mounts": [{"Type": "volume", "Name": "ct402-data", "Destination": "/srv"}],
                    }
                ]
            ),
            "/usr/bin/podman exec ct402-api wget -qO- http://127.0.0.1:8080/health": "CT402 recovered API",
            "/usr/bin/podman exec ct402-api cat /srv/health": "CT402 recovered API",
            module("CT402").SERVICE_COMMAND: "connected",
            "/usr/bin/curl --noproxy '*' --max-time 5 -fsS http://127.0.0.1:8080/health": "CT402 recovered API",
            "/usr/bin/cat /home/student/ct402-incident-report": "root cause: API_PORT was 9090 while published target was 8080\nrecovery: recreated ct402-api on 8080 with ct402-data preserved",
            "/usr/bin/cat /var/lib/labctl/ct402-fault-baseline": "staged-api-port=9090\npublished-target=8080",
            "/usr/bin/podman volume inspect ct402-data": json.dumps(
                [{"Name": "ct402-data", "CreatedAt": "2026-01-01T12:00:00Z"}]
            ),
            "/usr/bin/cat /var/lib/labctl/ct402-volume-baseline": json.dumps(
                [{"Name": "ct402-data", "CreatedAt": "2026-01-01T12:00:00Z"}]
            ),
        },
    )
