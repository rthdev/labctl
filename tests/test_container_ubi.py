"""UBI role/assignment contracts; no host engine or registry access."""

from __future__ import annotations

import pytest
import test_container_labs as catalog
import test_container_service_probe as service

BASE = "registry.access.redhat.com/ubi9/ubi:9.8"
HTTPD = "registry.access.redhat.com/ubi9/httpd-24:9.8"


@pytest.mark.parametrize("lab_id", catalog.CT_IDS)
def test_ubi_roles_and_offline_assignment(lab_id: str) -> None:
    directory = catalog.LABS / lab_id
    assignment = catalog.load_definition(directory / "lab.yaml").instructions
    images = {HTTPD} if lab_id == "CT101" else {BASE}
    if lab_id in {"CT202", "CT301", "CT402"}:
        images |= {HTTPD}
    for image in images:
        assert image in assignment
        assert f"podman pull {image}" in (directory / "setup.sh").read_text()
    for filename in ("lab.yaml", "setup.sh", "grade.py"):
        text = (directory / filename).read_text()
        assert "alpine" not in text.lower()
        assert "busybox" not in text.lower()
        assert "wget" not in text
    assert "--pull=never" in assignment
    assert "ct-image-provenance" in assignment
    assert "reset" in assignment


@pytest.mark.parametrize("lab_id", ["CT301", "CT402"])
def test_apache_static_contract_is_complete(lab_id: str) -> None:
    assignment = catalog.load_definition(catalog.LABS / lab_id / "lab.yaml").instructions
    assert service.probe(lab_id).HTTPD_CONFIG in assignment
    assert "/usr/sbin/httpd -f /tmp/ct-httpd.conf -DFOREGROUND" in assignment
    assert "1001" in assignment
    assert "no includes" in assignment
    assert "wait at least 3 seconds" in assignment
    assert "full restart, not a graceful Apache reload" in assignment
    assert "mtime and ctime" in assignment
    assert "relabel occurs at launch" in assignment
