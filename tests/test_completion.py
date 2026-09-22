import shlex
import shutil
import subprocess
from io import StringIO

import pytest

from labctl.cli import main


@pytest.mark.parametrize(
    ("words", "expected", "absent"),
    [
        (
            ["labctl", ""],
            {
                "lab",
                "vm",
                "image",
                "provider",
                "labs",
                "vms",
                "images",
                "providers",
                "version",
                "completion",
                "--json",
            },
            set(),
        ),
        (["labctl", "lab", ""], {"ls", "list", "create", "reconcile", "--uri"}, {"pull"}),
        (["labctl", "image", "pull", "--"], {"--force", "--json"}, {"--cached"}),
        (["labctl", "images", "--"], {"--cached", "--available"}, {"--force"}),
        (["labctl", "vms", "--"], {"--lab"}, {"--force"}),
        (["labctl", "--provider", "image", "lab", "list", "--"], {"--active"}, {"pull"}),
        (
            ["labctl", "lab", "--uri=qemu:///system", "create", "--"],
            {"--trust-external", "--allow-untrusted-image"},
            {"--active"},
        ),
        (["labctl", "vm", "stop", "LX001", "node", "--"], {"--force"}, {"--lab"}),
        (["labctl", "completion", ""], {"bash"}, set()),
        (["labctl", "lab", "ssh", "LX001", "--", ""], set(), {"--json", "lab"}),
        (["labctl", "lab", "--provider", ""], set(), {"create", "--force"}),
    ],
)
def test_standalone_bash_completion(tmp_path, words, expected, absent):
    out, err = StringIO(), StringIO()
    assert main(["completion", "bash"], stdout=out, stderr=err) == 0, err.getvalue()
    script = tmp_path / "labctl"
    script.write_text(out.getvalue())
    bash = shutil.which("bash")
    assert bash is not None
    subprocess.run([bash, "-n", str(script)], check=True)
    command = (
        'source "$1"; complete -p labctl; '
        f"COMP_WORDS=({' '.join(shlex.quote(w) for w in words)}); "
        f"COMP_CWORD={len(words) - 1}; "
        '_labctl_complete; printf "%s\\n" "${COMPREPLY[@]}"'
    )
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", command, "bash", str(script)],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = result.stdout.splitlines()
    assert "-F _labctl_complete labctl" in lines[0]
    candidates = set(lines[1:])
    assert expected <= candidates
    assert not absent & candidates


@pytest.mark.parametrize("argv", [["version", "--json"], ["completion", "bash", "--json"]])
def test_utility_json_matches_published_schema(argv):
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    out = StringIO()
    assert main(argv, stdout=out, stderr=StringIO()) == 0
    schema = json.loads(
        (Path(__file__).parents[1] / "docs/schemas/json-output-v1.schema.json").read_text()
    )
    Draft202012Validator(schema).validate(json.loads(out.getvalue()))
