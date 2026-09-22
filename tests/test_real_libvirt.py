"""Opt-in destructive real-libvirt lifecycle smoke test gate."""

from __future__ import annotations

import os

import pytest

from labctl.images import ImageCatalog
from labctl.kvm import KVMProvider


@pytest.mark.libvirt
def test_real_libvirt_prerequisites_are_explicit() -> None:
    if os.environ.get("LABCTL_REAL_LIBVIRT") != "1":
        pytest.skip("set LABCTL_REAL_LIBVIRT=1 only after all destructive-smoke prerequisites pass")
    unavailable = [image.id for image in ImageCatalog.bundled().values() if not image.available]
    if unavailable:
        pytest.skip("no verified pinned managed image is available: " + ", ".join(unavailable))
    health = KVMProvider().doctor()
    if not health.healthy:
        pytest.skip(
            "mandatory KVM doctor checks failed: "
            + "; ".join(check.detail for check in health.checks if check.status.value == "fail")
        )
    pytest.fail("real lifecycle smoke must be invoked by the verification harness, not this guard")
