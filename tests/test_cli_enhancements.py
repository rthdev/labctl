from io import StringIO

import pytest

from labctl.application import CommandResult
from labctl.cli import main, parse_args


@pytest.mark.parametrize(
    ("group", "options"),
    [
        ("image", ["--cached", "--available"]),
        ("provider", []),
        ("vm", ["--lab", "LX001"]),
        ("lab", ["--active"]),
    ],
)
@pytest.mark.parametrize("mode", [[], ["--json"], ["--quiet"], ["-v"]])
def test_plural_list_is_identical(group, options, mode):
    singular = [group, "ls", *options, *mode]
    plural = [group + "s", *options, *mode]
    assert vars(parse_args(plural)) == vars(parse_args(singular))

    class App:
        def execute(self, args):
            return CommandResult(group + "s", [{"id": "example"}], ("id",))

    outputs = []
    for argv in (singular, plural):
        out, err = StringIO(), StringIO()
        assert main(argv, application=App(), stdout=out, stderr=err) == 0
        outputs.append((out.getvalue(), err.getvalue()))
    assert outputs[0] == outputs[1]


def test_version_matches_project_without_application(tmp_path):
    import tomllib
    from pathlib import Path

    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    out = StringIO()
    assert main(["version"], stdout=out, stderr=StringIO()) == 0
    assert out.getvalue() == f"labctl {project['project']['version']}\n"


@pytest.mark.parametrize("cached", [True, False])
def test_verbose_inspect_path_only_in_human_mode(tmp_path, cached):
    import hashlib
    import json

    from labctl.application import Application
    from labctl.images import ImageStore

    app = Application(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
    )
    store = ImageStore(tmp_path / "cache/images")
    digest = hashlib.sha256(b"image").hexdigest()
    blob = store.blobs / digest
    if cached:
        blob.write_bytes(b"image")
        store.register_reference("rocky:9", digest, verified=True)
    for flags in ([], ["-v"], ["--json"], ["-v", "--json"]):
        out, err = StringIO(), StringIO()
        assert (
            main(["image", "inspect", *flags, "rocky:9"], application=app, stdout=out, stderr=err)
            == 0
        ), err.getvalue()
        text = out.getvalue()
        if "--json" in flags:
            assert "path" not in json.loads(text)["data"]
        elif "-v" in flags:
            assert f"path: {blob if cached else 'not cached'}" in text
        else:
            assert "path:" not in text
