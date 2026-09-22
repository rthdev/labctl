from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from labctl.images import ImageCatalog, ImageError, ImageMetadata, ImageStore
from labctl.subprocesses import CommandError, CommandResult


class FakeRunner:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.commands: list[tuple[str, ...]] = []

    def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
        command = tuple(argv)
        self.commands.append(command)
        result = CommandResult(command, self.returncode, '{"format":"qcow2"}', "invalid")
        if check and self.returncode:
            raise CommandError(result)
        return result


def test_bundled_metadata_is_honest_and_complete() -> None:
    catalog = ImageCatalog.bundled()

    assert set(catalog) == {"rocky:9", "rocky:10", "fedora:44"}
    expected = {
        "rocky:9": (
            "Rocky-9-GenericCloud-Base-9.8-20260525.0.x86_64.qcow2",
            "92c206cc6f790c61583247eefe87890f8828420662c17cacf247cec78ab4eec8",
            "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/",
        ),
        "rocky:10": (
            "Rocky-10-GenericCloud-Base-10.2-20260525.0.x86_64.qcow2",
            "9fc9e9ff16888bb68ac39b0392e25c9c92684d50c85f1cce6ab549363bbc4b48",
            "https://dl.rockylinux.org/pub/rocky/10/images/x86_64/",
        ),
        "fedora:44": (
            "Fedora-Server-Guest-Generic-44-1.7.x86_64.qcow2",
            "446c01f71e3c6cd3889af66fec927b8d1160b8e0744243d7861c2b8b2ddd3f0e",
            "https://download.fedoraproject.org/pub/fedora/linux/releases/44/Server/x86_64/images/",
        ),
    }
    for image_id, (filename, digest, base_url) in expected.items():
        image = catalog[image_id]
        assert image.format == "qcow2"
        assert image.available
        assert image.url == base_url + filename
        assert image.sha256 == digest
        assert image.build_identity == filename.removesuffix(".qcow2")
        assert image.checksum_source_url is not None
        assert image.unavailable_reason is None


def test_metadata_parser_rejects_inconsistent_or_insecure_entries() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        ImageMetadata.from_dict(
            {"id": "x:1", "format": "qcow2", "url": "http://example/x", "sha256": "a" * 64}
        )
    with pytest.raises(ValueError, match="SHA-256"):
        ImageMetadata.from_dict(
            {"id": "x:1", "format": "qcow2", "url": "https://example/x", "sha256": "bad"}
        )
    with pytest.raises(ValueError, match="reason"):
        ImageMetadata.from_dict({"id": "x:1", "format": "qcow2", "available": False})


def test_pull_is_content_addressed_atomic_and_reuses_verified_blob(tmp_path: Path) -> None:
    payload = b"qcow2 fixture"
    digest = hashlib.sha256(payload).hexdigest()
    metadata = ImageMetadata("test:1", "qcow2", "https://example.invalid/test.qcow2", digest)
    downloads = 0

    def download(url: str, destination: Path) -> None:
        nonlocal downloads
        downloads += 1
        assert url.startswith("https://")
        destination.write_bytes(payload)

    store = ImageStore(tmp_path, downloader=download)
    blob = store.pull(metadata)

    assert blob == tmp_path / "blobs" / "sha256" / digest
    assert blob.read_bytes() == payload
    assert json.loads((tmp_path / "refs" / "test%3A1.json").read_text())["sha256"] == digest
    assert not list((tmp_path / "tmp").iterdir())
    assert store.pull(metadata) == blob
    assert downloads == 1


def test_failed_or_corrupt_download_never_publishes_blob_or_reference(tmp_path: Path) -> None:
    metadata = ImageMetadata("test:1", "qcow2", "https://example.invalid/test.qcow2", "0" * 64)

    def download(_url: str, destination: Path) -> None:
        destination.write_bytes(b"wrong")

    store = ImageStore(tmp_path, downloader=download)
    with pytest.raises(ImageError, match="checksum"):
        store.pull(metadata)

    assert not list((tmp_path / "blobs" / "sha256").iterdir())
    assert not list((tmp_path / "refs").iterdir())
    assert not list((tmp_path / "tmp").iterdir())


def test_pull_refuses_unavailable_images_with_import_guidance(tmp_path: Path) -> None:
    image = ImageMetadata("rocky:9", "qcow2", unavailable_reason="not verified")

    with pytest.raises(ImageError) as caught:
        ImageStore(tmp_path).pull(image)

    message = str(caught.value)
    assert "not verified" in message
    assert "labctl image import NAME PATH --checksum SHA256" in message


def test_unavailable_image_guidance_does_not_interpolate_image_id(tmp_path: Path) -> None:
    image = ImageMetadata("$(malicious-command)", "qcow2", unavailable_reason="not verified")

    with pytest.raises(ImageError) as caught:
        ImageStore(tmp_path).pull(image)

    message = str(caught.value)
    assert "labctl image import NAME PATH --checksum SHA256" in message
    assert "labctl image import $(malicious-command)" not in message


def test_qemu_img_uses_argument_lists_for_check_info_and_import(tmp_path: Path) -> None:
    source = tmp_path / "source image.qcow2"
    source.write_bytes(b"image")
    runner = FakeRunner()
    store = ImageStore(tmp_path / "cache", runner=runner)

    assert store.qemu_info(source) == {"format": "qcow2"}
    store.qemu_check(source)
    blob = store.import_qcow2("local:test", source)

    assert runner.commands == [
        ("qemu-img", "info", "--output=json", str(source)),
        ("qemu-img", "check", "--output=json", str(source)),
        ("qemu-img", "info", "--output=json", str(source)),
        ("qemu-img", "check", "--output=json", str(source)),
    ]
    assert blob.read_bytes() == b"image"


def test_import_failure_does_not_publish(tmp_path: Path) -> None:
    source = tmp_path / "bad.qcow2"
    source.write_bytes(b"bad")
    store = ImageStore(tmp_path / "cache", runner=FakeRunner(returncode=1))

    with pytest.raises(CommandError):
        store.import_qcow2("bad:1", source)
    assert not list((tmp_path / "cache" / "blobs" / "sha256").iterdir())


def test_optional_trusted_checksum_and_forced_logical_delete_keep_blob(tmp_path: Path) -> None:
    source = tmp_path / "image.qcow2"
    source.write_bytes(b"image")
    digest = hashlib.sha256(b"image").hexdigest()
    runner = FakeRunner()
    store = ImageStore(tmp_path / "cache", runner=runner)

    blob = store.import_qcow2("local:test", source, trusted_sha256=digest)
    assert runner.commands == [
        ("qemu-img", "info", "--output=json", str(source)),
        ("qemu-img", "check", "--output=json", str(source)),
    ]
    assert store.references() == {"local:test": digest}
    with pytest.raises(ImageError, match="force"):
        store.delete("local:test")
    store.delete("local:test", force=True)

    assert store.references() == {}
    assert blob.exists()
