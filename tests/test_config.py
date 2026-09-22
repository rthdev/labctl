from pathlib import Path

import pytest

from labctl.config import Config, ConfigError, load_config
from labctl.paths import XDGPaths


def test_xdg_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "a"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    paths = XDGPaths.from_environment()
    assert paths.config == tmp_path / "c" / "labctl"
    assert paths.runtime.name == "labctl"


@pytest.mark.parametrize(
    "variable",
    [
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
    ],
)
def test_xdg_paths_reject_relative_environment_values(
    variable: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    environment = {"HOME": str(tmp_path / "home"), variable: "relative/path"}

    with pytest.raises(RuntimeError, match=rf"{variable} must be an absolute path"):
        XDGPaths.from_environment(environment)

    assert not (tmp_path / "relative/path").exists()


def test_config_precedence(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('provider = "file"\nshutdown_timeout = 20\n', encoding="utf-8")
    config = load_config(
        path,
        env={"LABCTL_PROVIDER": "env", "LABCTL_SHUTDOWN_TIMEOUT": "30"},
        cli={"provider": "cli"},
    )
    assert config.provider == "cli"
    assert config.shutdown_timeout == 30


def test_config_rejects_unknown_and_invalid(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("unknown = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown"):
        load_config(path)
    path.write_text("shutdown_timeout = -1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="shutdown_timeout"):
        load_config(path)


def test_libvirt_storage_root_default_and_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("labctl.config.os.getuid", lambda: 1234)
    assert Config().libvirt_storage_root == "/var/lib/libvirt/images/labctl/1234"

    path = tmp_path / "config.toml"
    path.write_text(f'libvirt_storage_root = "{tmp_path / "toml"}"\n', encoding="utf-8")
    config = load_config(
        path,
        env={"LABCTL_LIBVIRT_STORAGE_ROOT": str(tmp_path / "environment")},
    )
    assert config.libvirt_storage_root == str(tmp_path / "environment")


@pytest.mark.parametrize("value", ["relative", "", "."])
def test_libvirt_storage_root_must_be_absolute(value: str) -> None:
    with pytest.raises(ConfigError, match="libvirt_storage_root must be an absolute path"):
        load_config(env={"LABCTL_LIBVIRT_STORAGE_ROOT": value})
