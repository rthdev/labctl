from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from labctl.cloudinit import generate_cloud_init


def _files(monkeypatch, env):
    monkeypatch.setattr("os.environ", env)
    doc = yaml.safe_load(generate_cloud_init("ssh-ed25519 AAAA lab", ["true"]))
    return {entry["path"]: entry["content"] for entry in doc["write_files"]}


def test_generated_setup_and_login_preserve_literal_values(tmp_path, monkeypatch):
    marker = tmp_path / "executed"
    value = (
        f"""http://a:'\"$HOME${{HOME}}%x\\;$(touch {marker})`touch {marker}`#@proxy:3128/path """
    )
    files = _files(monkeypatch, {"https_proxy": value})
    data = tmp_path / "proxy.json"
    data.write_text(files["/etc/labctl/proxy.json"])
    setup = tmp_path / "setup"
    result = tmp_path / "result.json"
    setup.write_text(
        f"#!{sys.executable}\nimport json, os\n"
        f'open({str(result)!r}, "w").write(json.dumps(dict(os.environ)))\n'
    )
    setup.chmod(0o700)
    wrapper = (
        files["/usr/local/sbin/labctl-proxy-setup"]
        .replace("/etc/labctl/proxy.json", str(data))
        .replace("/usr/local/sbin/labctl-setup", str(setup))
        .replace("/usr/local/sbin/labctl-proxy-login", "/bin/true")
    )
    subprocess.run([sys.executable, "-c", wrapper], check=True, env={"PATH": os.defpath})
    assert json.loads(result.read_text())["https_proxy"] == value
    assert json.loads(result.read_text())["HTTPS_PROXY"] == value
    profile = tmp_path / "profile"
    profile.write_text(files["/etc/profile.d/labctl-proxy.sh"])
    probe = subprocess.run(
        [
            "/bin/sh",
            "-c",
            '. "$1"; exec "$2" -c \'import json,os; print(json.dumps(dict(os.environ)))\'',
            "sh",
            str(profile),
            sys.executable,
        ],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.defpath},
    )
    assert json.loads(probe.stdout)["https_proxy"] == value
    # OpenSSH SetEnv uses double-quoted argv, not shell execution/expansion.
    settings = shlex.split(_install_login(files, tmp_path).read_text())
    assert settings[0] == "SetEnv"
    assert dict(item.split("=", 1) for item in settings[1:])["https_proxy"] == value
    generator = files["/etc/systemd/user-environment-generators/90-labctl-proxy"].replace(
        "/etc/labctl/proxy.json", str(data)
    )
    generated = subprocess.run(
        [sys.executable, "-c", generator],
        check=True,
        text=True,
        capture_output=True,
        env={"PATH": os.defpath},
    )
    assert (
        dict(token.split("=", 1) for token in shlex.split(generated.stdout))["https_proxy"] == value
    )
    sudo = files["/etc/sudoers.d/90-labctl-proxy"]
    assert 'Defaults env_keep += "' in sudo
    assert "https_proxy HTTPS_PROXY" in sudo
    assert value not in sudo
    assert not marker.exists()


def test_login_installer_fails_closed_when_image_ignores_dropin(tmp_path, monkeypatch):
    files = _files(monkeypatch, {"http_proxy": "secret-fixture"})
    data = tmp_path / "proxy.json"
    data.write_text(files["/etc/labctl/proxy.json"])
    target = tmp_path / "ignored.conf"
    installer = (
        files["/usr/local/sbin/labctl-proxy-login"]
        .replace("/etc/labctl/proxy.json", str(data))
        .replace("/etc/ssh/sshd_config.d/00-labctl-proxy.conf", str(target))
        .replace("/usr/sbin/sshd", "/bin/true")
        .replace("/usr/bin/systemctl", "/bin/true")
    )
    result = subprocess.run([sys.executable, "-c", installer], capture_output=True, text=True)
    assert result.returncode != 0
    assert result.stderr.strip() == "labctl: proxy login configuration failed"
    assert "secret-fixture" not in result.stderr + result.stdout


def _install_login(files, tmp_path, vendor=""):
    data = tmp_path / "login-proxy.json"
    data.write_text(files["/etc/labctl/proxy.json"])
    output = tmp_path / "sshd_config"
    sshd = tmp_path / "sshd"
    sshd.write_text(
        f"#!{sys.executable}\nimport shlex\nfrom pathlib import Path\n"
        f"p = Path({str(output)!r})\n"
        f"if not p.exists():\n    print({vendor!r})\n"
        "else:\n    for item in shlex.split(p.read_text())[1:]:\n        print('setenv ' + item)\n"
    )
    sshd.chmod(0o700)
    systemctl = tmp_path / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o700)
    installer = (
        files["/usr/local/sbin/labctl-proxy-login"]
        .replace("/etc/labctl/proxy.json", str(data))
        .replace("/etc/ssh/sshd_config.d/00-labctl-proxy.conf", str(output))
        .replace("/usr/sbin/sshd", str(sshd))
        .replace("/usr/bin/systemctl", str(systemctl))
    )
    subprocess.run([sys.executable, "-c", installer], check=True, capture_output=True)
    return output


def test_ssh_login_installer_merges_vendor_environment_in_sandbox(tmp_path, monkeypatch):
    files = _files(monkeypatch, {"http_proxy": "http://new:'\"$%#@proxy:3128"})
    assert "/etc/ssh/sshd_config.d/00-labctl-proxy.conf" not in files
    output = _install_login(files, tmp_path, "setenv VENDOR=unchanged value\nsetenv http_proxy=old")
    values = dict(item.split("=", 1) for item in shlex.split(output.read_text())[1:])
    assert values["VENDOR"] == "unchanged value"
    assert values["http_proxy"] == "http://new:'\"$%#@proxy:3128"


def test_bundled_ct_initial_package_and_runuser_pull_inherit_proxy(tmp_path, monkeypatch):
    files = _files(monkeypatch, {"https_proxy": "http://fixture-secret@proxy:3128"})
    data = tmp_path / "proxy.json"
    data.write_text(files["/etc/labctl/proxy.json"])
    log = tmp_path / "observed.jsonl"
    probe = (
        f"#!{sys.executable}\nimport os,json\n"
        f'with open({str(log)!r}, "a") as f: f.write(json.dumps(dict(os.environ)) + "\\n")\n'
    )
    for name in ("dnf", "podman"):
        executable = tmp_path / name
        executable.write_text(probe)
        executable.chmod(0o700)
    # Rootless switching is mocked, not a host account or container operation.
    # Keep the real runuser argv shape and exec the actual env utility unchanged.
    runuser = tmp_path / "runuser"
    runuser.write_text(
        f"#!{sys.executable}\nimport os,sys\n"
        'assert sys.argv[1:4] == ["-u", "student", "--"]\n'
        "os.execvp(sys.argv[4],sys.argv[4:])\n"
    )
    runuser.chmod(0o700)
    bundled = Path(__file__).parents[1] / "src/labctl/data/labs/CT001/setup.sh"
    lines = bundled.read_text().splitlines()
    package = next(line for line in lines if line.startswith("dnf "))
    pull = next(
        line.strip()
        for line in lines
        if line.strip().startswith("runuser ") and "podman pull" in line
    )
    setup = tmp_path / "setup"
    setup.write_text(
        "#!/bin/sh\nset -eu\nuid=1000\n"
        + package
        + "\n"
        + pull.replace("/usr/bin/podman", str(tmp_path / "podman"))
        + "\n"
    )
    setup.chmod(0o700)
    wrapper = (
        files["/usr/local/sbin/labctl-proxy-setup"]
        .replace("/etc/labctl/proxy.json", str(data))
        .replace("/usr/local/sbin/labctl-setup", str(setup))
        .replace("/usr/local/sbin/labctl-proxy-login", "/bin/true")
    )
    subprocess.run(
        [sys.executable, "-c", wrapper],
        check=True,
        env={"PATH": str(tmp_path) + os.pathsep + os.defpath},
    )
    observations = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(observations) == 2
    for entry in observations:
        assert entry["https_proxy"] == entry["HTTPS_PROXY"] == "http://fixture-secret@proxy:3128"
    assert observations[1]["HOME"] == "/home/student"


@pytest.mark.parametrize(
    "name", ["http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "no_proxy"]
)
def test_uppercase_fallback_and_explicit_empty_lowercase(monkeypatch, name):
    files = _files(monkeypatch, {name.upper(): "upper"})
    values = json.loads(files["/etc/labctl/proxy.json"])
    assert values[name] == values[name.upper()] == "upper"
    files = _files(monkeypatch, {name: "", name.upper(): "upper"})
    values = json.loads(files["/etc/labctl/proxy.json"])
    assert values[name] == values[name.upper()] == ""
    if name != "no_proxy":
        assert "no_proxy" not in values


def test_no_proxy_environment_keeps_original_cloud_init(monkeypatch):
    files = _files(monkeypatch, {"TOKEN": "not-copied"})
    assert list(files) == ["/usr/local/sbin/labctl-setup"]
    doc = yaml.safe_load(generate_cloud_init("ssh-ed25519 AAAA lab", ["true"]))
    assert doc["runcmd"] == [["/usr/local/sbin/labctl-setup"]]


def test_bypass_preserves_user_text_before_mandatory_local_additions(monkeypatch):
    value = " .example.test,127.0.0.1,192.0.2.7 "
    files = _files(
        monkeypatch, {"all_proxy": "socks5://proxy:1080", "no_proxy": value, "NO_PROXY": "ignored"}
    )
    values = json.loads(files["/etc/labctl/proxy.json"])
    assert values["no_proxy"] == values["NO_PROXY"] == value + ",localhost,127.0.0.1,::1"


def test_guest_wrapper_errors_do_not_expose_data(tmp_path, monkeypatch):
    files = _files(monkeypatch, {"http_proxy": "secret"})
    data = tmp_path / "invalid.json"
    data.write_text("secret-invalid-json")
    wrapper = files["/usr/local/sbin/labctl-proxy-setup"].replace(
        "/etc/labctl/proxy.json", str(data)
    )
    result = subprocess.run([sys.executable, "-c", wrapper], capture_output=True, text=True)
    assert result.returncode != 0
    assert result.stderr.strip() == "labctl: proxy setup failed"
    assert "secret" not in result.stdout + result.stderr


def test_openssh_parses_literal_proxy_values(tmp_path, monkeypatch):
    import shutil

    sshd = shutil.which("sshd")
    keygen = shutil.which("ssh-keygen")
    if not sshd or not keygen:
        pytest.skip("OpenSSH parser not installed")
    value = """http://a:'"$HOME${HOME}%x\\;$(false)`false`#@proxy:3128/path """
    files = _files(monkeypatch, {"https_proxy": value})
    config = _install_login(files, tmp_path)
    key = tmp_path / "key"
    subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    result = subprocess.run(
        [sshd, "-T", "-f", str(config), "-h", str(key)], capture_output=True, text=True, check=True
    )
    entries = dict(
        line.removeprefix("setenv ").split("=", 1)
        for line in result.stdout.splitlines()
        if line.startswith("setenv ")
    )
    assert entries["https_proxy"] == entries["HTTPS_PROXY"] == value


def test_systemd_parses_generator_literal_values(tmp_path, monkeypatch):
    import ctypes
    import glob

    libraries = glob.glob("/usr/lib*/systemd/libsystemd-shared-*.so") + glob.glob(
        "/usr/lib/*/systemd/libsystemd-shared-*.so"
    )
    if not libraries:
        pytest.skip("systemd shared parser not installed")
    library = ctypes.CDLL(libraries[0])
    if not hasattr(library, "load_env_file"):
        pytest.skip("systemd shared parser not exported")
    value = """http://a:'"$HOME${HOME}%x\\;$(false)`false`#@proxy:3128/path """
    files = _files(monkeypatch, {"https_proxy": value})
    data = tmp_path / "proxy.json"
    data.write_text(files["/etc/labctl/proxy.json"])
    generator = files["/etc/systemd/user-environment-generators/90-labctl-proxy"].replace(
        "/etc/labctl/proxy.json", str(data)
    )
    result = subprocess.run(
        [sys.executable, "-c", generator], capture_output=True, text=True, check=True
    )
    output = tmp_path / "environment"
    output.write_text(result.stdout)
    array = ctypes.POINTER(ctypes.c_char_p)()
    library.load_env_file.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p)),
    ]
    library.load_env_file.restype = ctypes.c_int
    assert library.load_env_file(None, os.fsencode(output), ctypes.byref(array)) == 0
    try:
        parsed = {}
        index = 0
        while array[index]:
            name, val = array[index].decode().split("=", 1)
            parsed[name] = val
            index += 1
        assert parsed["https_proxy"] == parsed["HTTPS_PROXY"] == value
    finally:
        library.strv_free.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
        library.strv_free(array)


@pytest.mark.parametrize(
    "control", ["\n", "\r", "\x00", "\t", "\x1b", "\x7f", "\x85", "\u2028", "\u2029", "\udcff"]
)
def test_control_values_fail_without_disclosing_value(monkeypatch, control):
    with pytest.raises(ValueError, match="unsupported control") as error:
        _files(monkeypatch, {"http_proxy": "credential" + control + "secret"})
    assert "credential" not in str(error.value)
    assert "secret" not in str(error.value)


def test_public_cloud_init_inherits_only_harmonized_proxy_environment(monkeypatch):
    monkeypatch.setattr(
        "os.environ",
        {"http_proxy": "http://proxy:3128", "HTTP_PROXY": "ignored", "TOKEN": "secret"},
    )
    doc = yaml.safe_load(generate_cloud_init("ssh-ed25519 AAAA lab", ["true"]))
    files = {entry["path"]: entry["content"] for entry in doc["write_files"]}
    assert "/etc/labctl/proxy.json" in files
    import json

    assert json.loads(files["/etc/labctl/proxy.json"]) == {
        "http_proxy": "http://proxy:3128",
        "HTTP_PROXY": "http://proxy:3128",
        "no_proxy": "localhost,127.0.0.1,::1",
        "NO_PROXY": "localhost,127.0.0.1,::1",
    }
    assert doc["runcmd"] == [["/usr/local/sbin/labctl-proxy-setup"]]
