"""Verified image metadata and content-addressed local image storage."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from labctl.subprocesses import CommandRunner, Runner


class ImageError(RuntimeError):
    """An image operation could not be completed safely."""


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    id: str
    format: str
    url: str | None = None
    sha256: str | None = None
    unavailable_reason: str | None = None
    build_identity: str | None = None
    architecture: str = "x86_64"
    checksum_algorithm: str = "sha256"
    checksum_source_url: str | None = None
    default_ssh_user: str = ""

    @property
    def logical_name(self) -> str:
        return self.id

    @property
    def availability(self) -> str:
        return "available" if self.available else "unavailable"

    def __post_init__(self) -> None:
        if not self.id or self.format != "qcow2":
            raise ValueError("image ID is required and format must be qcow2")
        if self.url is not None and urlparse(self.url).scheme != "https":
            raise ValueError("available image URLs must use HTTPS")
        if self.sha256 is not None and (
            len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256)
        ):
            raise ValueError("image SHA-256 must be 64 lowercase hexadecimal characters")
        if (self.url is None) != (self.sha256 is None):
            raise ValueError("available images require both URL and SHA-256")
        if self.url is None and not self.unavailable_reason:
            raise ValueError("unavailable images require an honest reason")
        if self.url is not None and self.unavailable_reason is not None:
            raise ValueError("available images cannot have an unavailable reason")

    @property
    def available(self) -> bool:
        return self.url is not None

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ImageMetadata:
        def optional_string(name: str) -> str | None:
            item = value.get(name)
            if item is None:
                return None
            if not isinstance(item, str):
                raise ValueError(f"image {name} must be a string")
            return item

        image_id = value.get("id")
        image_format = value.get("format", "qcow2")
        if not isinstance(image_id, str) or not isinstance(image_format, str):
            raise ValueError("image id and format must be strings")
        return cls(
            image_id,
            image_format,
            optional_string("url"),
            optional_string("sha256"),
            optional_string("unavailable_reason"),
            optional_string("build_identity"),
            optional_string("architecture") or "x86_64",
            optional_string("checksum_algorithm") or "sha256",
            optional_string("checksum_source_url"),
            optional_string("default_ssh_user") or "",
        )


class ImageCatalog(Mapping[str, ImageMetadata]):
    def __init__(self, images: list[ImageMetadata]) -> None:
        self._images: dict[str, ImageMetadata] = {}
        for image in images:
            if image.id in self._images:
                raise ValueError(f"duplicate image ID {image.id!r}")
            self._images[image.id] = image

    def __getitem__(self, key: str) -> ImageMetadata:
        return self._images[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._images)

    def __len__(self) -> int:
        return len(self._images)

    @classmethod
    def parse(cls, content: str) -> ImageCatalog:
        decoded: Any = json.loads(content)
        if not isinstance(decoded, dict) or not isinstance(decoded.get("images"), list):
            raise ValueError("image metadata must contain an images list")
        entries: list[ImageMetadata] = []
        for item in decoded["images"]:
            if not isinstance(item, dict):
                raise ValueError("each image metadata entry must be an object")
            entries.append(ImageMetadata.from_dict(item))
        return cls(entries)

    @classmethod
    def bundled(cls) -> ImageCatalog:
        resource = files("labctl").joinpath("data/images/metadata.json")
        return cls.parse(resource.read_text(encoding="utf-8"))


Downloader = Callable[[str, Path], None]
DownloadProgress = Callable[[int, int | None], None]


class _HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if urlparse(newurl).scheme != "https":
            raise ImageError("image download redirects require HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, destination: Path, *, progress: DownloadProgress | None = None) -> None:
    if urlparse(url).scheme != "https":
        raise ImageError("image downloads require HTTPS")
    request = urllib.request.Request(  # noqa: S310 - HTTPS was required above
        url, headers={"User-Agent": "labctl/0.1"}
    )
    # Keep urllib's default ProxyHandler: environment proxies, bypass rules,
    # lowercase precedence, and HTTPS CONNECT/TLS validation remain standard.
    opener = urllib.request.build_opener(_HTTPSRedirectHandler())
    with opener.open(request) as response, destination.open("wb") as output:
        try:
            total = int(response.headers.get("Content-Length", ""))
        except ValueError:
            total = 0
        expected = total if total > 0 else None
        downloaded = 0
        if progress is not None:
            progress(downloaded, expected)
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            downloaded += len(chunk)
            if progress is not None:
                progress(
                    downloaded, expected if expected is None or downloaded <= expected else None
                )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ImageStore:
    """Content-addressed blobs with atomic, lock-protected logical references."""

    def __init__(
        self,
        root: Path,
        *,
        runner: CommandRunner | None = None,
        downloader: Downloader | None = None,
        progress: DownloadProgress | None = None,
    ) -> None:
        self.root = root
        self._runner = runner or Runner()
        self._downloader = downloader or (
            lambda url, destination: _download(url, destination, progress=progress)
        )
        for directory in (self.blobs, self.refs, self.tmp, self.locks):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)

    @property
    def blobs(self) -> Path:
        return self.root / "blobs" / "sha256"

    @property
    def refs(self) -> Path:
        return self.root / "refs"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def locks(self) -> Path:
        return self.root / "locks"

    def _ref_path(self, image_id: str) -> Path:
        return self.refs / f"{quote(image_id, safe='')}.json"

    @contextmanager
    def _locked(self, image_id: str) -> Iterator[None]:
        lock_path = self.locks / f"{quote(image_id, safe='')}.lock"
        with lock_path.open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ImageError(f"image is locked by another operation: {image_id}") from exc
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _publish_ref(self, image_id: str, digest: str, *, verified: bool = True) -> None:
        target = self._ref_path(image_id)
        descriptor, temporary_name = tempfile.mkstemp(dir=self.tmp, prefix="ref-")
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(
                    {"id": image_id, "sha256": digest, "verified": verified},
                    output,
                    sort_keys=True,
                )
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def pull(self, image: ImageMetadata) -> Path:
        if not image.available:
            raise ImageError(
                f"image {image.id!r} is unavailable: {image.unavailable_reason} "
                "Import a trusted local QCOW2 under that image ID instead: "
                "labctl image import NAME PATH --checksum SHA256"
            )
        if image.url is None or image.sha256 is None:
            raise ImageError(f"image {image.id!r} has incomplete metadata")
        with self._locked(image.id):
            blob = self.blobs / image.sha256
            if blob.exists():
                if _sha256(blob) != image.sha256:
                    raise ImageError(f"cached blob for {image.id!r} has the wrong checksum")
                self._publish_ref(image.id, image.sha256)
                return blob

            descriptor, temporary_name = tempfile.mkstemp(dir=self.tmp, prefix="pull-")
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                self._downloader(image.url, temporary)
                actual = _sha256(temporary)
                if actual != image.sha256:
                    raise ImageError(
                        f"checksum mismatch for {image.id!r}: expected {image.sha256}, got {actual}"
                    )
                os.replace(temporary, blob)
                os.chmod(blob, 0o444)
                self._publish_ref(image.id, image.sha256)
                return blob
            finally:
                temporary.unlink(missing_ok=True)

    def qemu_check(self, path: Path) -> None:
        self._runner.run(["qemu-img", "check", "--output=json", str(path)])

    def qemu_info(self, path: Path) -> dict[str, object]:
        result = self._runner.run(["qemu-img", "info", "--output=json", str(path)])
        decoded: Any = json.loads(result.stdout)
        if not isinstance(decoded, dict):
            raise ImageError("qemu-img info did not return a JSON object")
        return decoded

    def import_qcow2(
        self, image_id: str, source: Path, *, trusted_sha256: str | None = None
    ) -> Path:
        if not source.is_file():
            raise ImageError(f"image source does not exist: {source}")
        actual = _sha256(source)
        if trusted_sha256 is not None and actual != trusted_sha256:
            raise ImageError(f"trusted checksum mismatch: expected {trusted_sha256}, got {actual}")
        info = self.qemu_info(source)
        image_format = info.get("format")
        if image_format != "qcow2":
            raise ImageError(f"expected qcow2 input, qemu-img reported {image_format!r}")
        self.qemu_check(source)
        with self._locked(image_id):
            blob = self.blobs / actual
            if not blob.exists():
                descriptor, temporary_name = tempfile.mkstemp(dir=self.tmp, prefix="import-")
                os.close(descriptor)
                temporary = Path(temporary_name)
                try:
                    shutil.copyfile(source, temporary)
                    if _sha256(temporary) != actual:
                        raise ImageError("image source changed during import")
                    os.replace(temporary, blob)
                    os.chmod(blob, 0o444)
                finally:
                    temporary.unlink(missing_ok=True)
            self._publish_ref(image_id, actual, verified=trusted_sha256 is not None)
            return blob

    def register_reference(self, image_id: str, digest: str, *, verified: bool) -> None:
        """Register an existing immutable blob after validating its digest."""
        blob = self.blobs / digest
        if not blob.is_file() or _sha256(blob) != digest:
            raise ImageError(f"blob does not match digest for {image_id!r}")
        os.chmod(blob, 0o444)
        with self._locked(image_id):
            self._publish_ref(image_id, digest, verified=verified)

    def resolve(self, image_id: str) -> tuple[Path, str, bool]:
        """Resolve a logical image to an immutable blob and its trust decision."""
        reference = self._ref_path(image_id)
        try:
            decoded: Any = json.loads(reference.read_text(encoding="utf-8"))
            digest = decoded["sha256"]
            verified = decoded.get("verified", False)
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ImageError(f"image is not cached: {image_id}") from exc
        if not isinstance(digest, str) or not isinstance(verified, bool):
            raise ImageError(f"invalid image reference: {reference}")
        blob = self.blobs / digest
        if not blob.is_file() or _sha256(blob) != digest:
            raise ImageError(f"image blob is missing or corrupt: {image_id}")
        return blob, digest, verified

    def references(self) -> dict[str, str]:
        references: dict[str, str] = {}
        for path in sorted(self.refs.glob("*.json")):
            decoded: Any = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(decoded, dict)
                or not isinstance(decoded.get("id"), str)
                or not isinstance(decoded.get("sha256"), str)
            ):
                raise ImageError(f"invalid image reference: {path}")
            references[decoded["id"]] = decoded["sha256"]
        return references

    def delete(self, image_id: str, *, force: bool = False) -> None:
        """Delete a logical reference only; immutable blobs are retained for safe reuse."""

        reference = self._ref_path(image_id)
        if not reference.exists():
            raise ImageError(f"image reference does not exist: {image_id}")
        if not force:
            raise ImageError("logical image deletion requires force=True")
        with self._locked(image_id):
            reference.unlink(missing_ok=True)
