"""Run complete negative shell predicates with local paths, never real guests."""

import runpy
import shlex
import subprocess
from pathlib import Path

import pytest

LABS = Path(__file__).parents[1] / "src/labctl/data/labs"


def predicate(lab, index):
    return shlex.split(runpy.run_path(str(LABS / lab / "grade.py"))["CHECKS"][index][1])[-1]


def execute(command, fixture=""):
    return subprocess.run(
        ["/usr/bin/bash", "-euo", "pipefail", "-c", fixture + command],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


@pytest.mark.parametrize(
    "options,grep_rc,accepted",
    [
        ("defaults", None, True),
        ("rw,relatime,inode64", None, True),
        ("defaults,errors=remount-ro", None, True),
        ("ro,noauto,nofail", None, False),
        ("ro,defaults", None, False),
        ("defaults,noauto,rw", None, False),
        ("defaults,ro", None, False),
        ("defaults,noauto", None, False),
        ("defaults,nofail", None, False),
        ("defaults", 2, False),
        ("defaults", 42, False),
    ],
)
def test_lx210_negative_mount_options_full_predicate(options, grep_rc, accepted):
    fixture = lx210_mount_fixture(options)
    if grep_rc is not None:
        fixture += f"grep() {{ cat >/dev/null; return {grep_rc}; }}\n"
    result = execute(predicate("LX210", 2), fixture)
    assert (result.returncode == 0 and result.stdout.strip() == "ok") is accepted, result


def lx210_mount_fixture(options):
    return r"""
losetup() { echo /dev/loop999; }
lvs() { echo 402653184; }
readlink() { echo "${@: -1}"; }
findmnt() {
    case "${@: -1}" in
        SOURCE) echo /dev/vg210/records;;
        FSTYPE) echo xfs;;
        OPTIONS) echo OPTIONS_VALUE;;
        *) return 2;;
    esac
}
""".replace("OPTIONS_VALUE", shlex.quote(options))


@pytest.mark.parametrize(
    "original,replacement",
    [
        ("SOURCE) echo /dev/vg210/records", "SOURCE) echo /dev/other/records"),
        ("FSTYPE) echo xfs", "FSTYPE) echo ext4"),
        ("OPTIONS) echo defaults", "OPTIONS) return 2"),
    ],
)
def test_lx210_persistent_mount_prerequisites(original, replacement):
    fixture = lx210_mount_fixture("defaults").replace(original, replacement)
    result = execute(predicate("LX210", 2), fixture)
    assert result.returncode != 0, result
    assert "ok" not in result.stdout, result


@pytest.mark.parametrize("producer", ["printf", "tr"])
@pytest.mark.parametrize("error_rc", [2, 42])
@pytest.mark.parametrize("options", ["defaults", "defaults,ro"])
def test_lx210_mount_pipeline_producer_error_with_real_grep(producer, error_rc, options):
    fixture = lx210_mount_fixture(options)
    if producer == "tr":
        fixture += f"tr() {{ cat >/dev/null; return {error_rc}; }}\n"
    else:
        fixture += f"printf() {{ return {error_rc}; }}\n"
    # No grep shim: real grep returns no-match after the producer emits no tokens.
    result = execute(predicate("LX210", 2), fixture)
    assert result.returncode != 0, result
    assert "ok" not in result.stdout, result


@pytest.mark.parametrize("producer", ["printf", "tr"])
@pytest.mark.parametrize("error_rc", [1, 2, 42])
@pytest.mark.parametrize("output", ["", "defaults", "ro"])
def test_lx210_pipeline_partial_output_failure(producer, error_rc, output):
    fixture = lx210_mount_fixture("defaults,ro")
    consume = "cat >/dev/null; " if producer == "tr" else ""
    fixture += (
        f"{producer}() {{ {consume}builtin printf '%s\\n' {shlex.quote(output)}; "
        f"return {error_rc}; }}\n"
    )
    result = execute(predicate("LX210", 2), fixture)
    assert result.returncode != 0, result
    assert "ok" not in result.stdout, result


@pytest.mark.parametrize("state", ["clean", "selinux=0", "enforcing=0", "missing", "directory"])
def test_lx310_selinux_full_predicate(tmp_path, state):
    config = tmp_path / "config"
    config.write_text("SELINUX=enforcing\n")
    cmdline = tmp_path / "cmdline"
    if state == "directory":
        cmdline.mkdir()
    elif state != "missing":
        cmdline.write_text("quiet " + ("" if state == "clean" else state) + "\n")
    command = predicate("LX310", 5)
    for old, new in {
        "/etc/selinux/config": config,
        "/proc/cmdline": cmdline,
        "/etc/shadow": tmp_path / "shadow",
        "/.autorelabel": tmp_path / "autorelabel",
    }.items():
        command = command.replace(old, str(new))
    result = execute(
        command,
        "getenforce() { echo Enforcing; }\n"
        "stat() { echo shadow_t; }\nmatchpathcon() { echo shadow_t; }\n",
    )
    assert (result.returncode == 0 and result.stdout.strip() == "ok") is (state == "clean"), result


@pytest.mark.parametrize("lab,index,expected", [("LX211", 1, "ok"), ("LX412", 3, "verified")])
@pytest.mark.parametrize(
    "state",
    [
        "clean",
        "permissive",
        "grep2",
        "grep42",
        "semanage1",
        "semanage2",
        "semanage42",
        "config-missing",
        "config-directory",
        "config-malformed",
    ],
)
def test_permissive_domain_full_predicate(tmp_path, lab, index, expected, state):
    config = tmp_path / "config"
    config.write_text("SELINUX=enforcing\n")
    release = tmp_path / "release"
    release.write_text("release\n")
    context = tmp_path / "context"
    context.write_text("system_u:system_r:httpd_t:s0\n")
    evidence = config if lab == "LX211" else context
    if state == "config-missing":
        evidence.unlink()
    elif state == "config-directory":
        evidence.unlink()
        evidence.mkdir()
    elif state == "config-malformed":
        evidence.write_text("invalid\n")
    command = predicate(lab, index).replace("/etc/selinux/config", str(config))
    command = command.replace("/srv/lx412/public/release.txt", str(release))
    command = command.replace("/proc/$pid/attr/current", str(context))
    fixture = """
getenforce() { echo Enforcing; }
getsebool() { echo 'httpd_unified --> off'; }
curl() { echo release; }
systemctl() { echo 1234; }
"""
    fixture += (
        "semanage() { echo " + ("httpd_t" if state == "permissive" else "unconfined_t") + "; }\n"
    )
    if state.startswith("semanage"):
        fixture += f"semanage() {{ echo unconfined_t; return {state[8:]}; }}\n"
    if state.startswith("grep"):
        fixture += (
            'grep() { case "${@: -1}" in '
            + shlex.quote(str(config))
            + "|"
            + shlex.quote(str(context))
            + f') command grep "$@";; *) cat >/dev/null; return {state[4:]};; esac; }}\n'
        )
    result = execute(command, fixture)
    assert (result.returncode == 0 and result.stdout.strip() == expected) is (state == "clean"), (
        result
    )


@pytest.mark.parametrize("error_rc", [1, 2, 42])
@pytest.mark.parametrize("output", ["", "unconfined_t", "httpd_t"])
def test_lx211_negative_pipeline_printf_failure(tmp_path, error_rc, output):
    config = tmp_path / "config"
    config.write_text("SELINUX=enforcing\n")
    command = predicate("LX211", 1).replace("/etc/selinux/config", str(config))
    fixture = (
        "getenforce() { echo Enforcing; }\n"
        "getsebool() { echo 'httpd_unified --> off'; }\n"
        "semanage() { echo httpd_t; }\n"
        f"printf() {{ builtin printf '%s\\n' {shlex.quote(output)}; return {error_rc}; }}\n"
    )
    result = execute(command, fixture)
    assert result.returncode != 0, result
    assert "ok" not in result.stdout, result


@pytest.mark.parametrize("target", ["grub", "cmdline", "first", "last"])
@pytest.mark.parametrize("state", ["clean", "rescue", "missing", "directory", "grep2", "grep42"])
def test_lx310_boot_policy_full_predicate(tmp_path, target, state):
    paths = {
        "grub": tmp_path / "grub",
        "cmdline": tmp_path / "cmdline",
        "first": tmp_path / "entries/a.conf",
        "last": tmp_path / "entries/z.conf",
    }
    (tmp_path / "entries").mkdir()
    for path in paths.values():
        path.write_text("options quiet\n")
    (tmp_path / "baseline").write_text("a.conf\nz.conf\n")
    path = paths[target]
    if state == "rescue":
        path.write_text("options systemd.unit=rescue.target\n")
    elif state in {"missing", "directory"}:
        path.unlink()
        if state == "directory":
            path.mkdir()
    command = predicate("LX310", 2)
    for old, new in {
        "/etc/default/grub": paths["grub"],
        "/proc/cmdline": paths["cmdline"],
        "/boot/loader/entries": tmp_path / "entries",
        "/var/lib/labctl/lx310-bls": tmp_path / "baseline",
    }.items():
        command = command.replace(old, str(new))
    fixture = "systemctl() { echo multi-user.target; }\n"
    if state.startswith("grep"):
        fixture += (
            'grep() { if [[ "${@: -1}" = '
            + shlex.quote(str(path))
            + f' ]]; then return {state[4:]}; else command grep "$@"; fi; }}\n'
        )
    result = execute(command, fixture)
    assert (result.returncode == 0 and result.stdout.strip() == "ok") is (state == "clean"), result
