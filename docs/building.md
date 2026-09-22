# Python builds

labctl is packaged only as a Python wheel and source distribution (sdist), using
Hatchling through `pyproject.toml`. Install a reviewed checkout or an explicit
wheel path with pipx; do not use bare `pipx install labctl`, which resolves to an
unrelated PyPI project. Host dependencies such as libvirt and QEMU remain
installed through the distribution package manager; see the installation guides
linked from the README.

## Python Artifacts

Use an isolated environment and pin the build frontend in the build environment:

```console
SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)
export SOURCE_DATE_EPOCH PYTHONHASHSEED=0 TZ=UTC LC_ALL=C.UTF-8
python3.12 -m pip install 'build==1.3.0'
python3.12 -m build --sdist --wheel
python3.12 -m pip install --force-reinstall dist/labctl-0.1.0-py3-none-any.whl
python3.12 -m pytest
```

The Hatch configuration includes `src/labctl/data/**`; both lab YAML files and
executable scripts are therefore wheel/sdist inputs. Compare SHA-256 results
only between clean checkouts using the same Python, Hatchling, build frontend,
umask, and `SOURCE_DATE_EPOCH`.
