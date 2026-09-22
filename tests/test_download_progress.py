import hashlib
from io import BytesIO, StringIO

import pytest

from labctl.application import Application
from labctl.cli import main
from labctl.images import ImageCatalog, ImageMetadata


class Tty(StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("length", ["6", None, "invalid", "-1"])
@pytest.mark.parametrize("mode", ["tty", "verbose", "pipe", "json", "quiet", "debug"])
@pytest.mark.parametrize("failure", [False, True])
def test_real_download_progress_and_cleanup(tmp_path, monkeypatch, length, mode, failure):
    import urllib.request

    class Response(BytesIO):
        headers = {} if length is None else {"Content-Length": length}

        def read(self, size=-1):
            if failure and self.tell() >= 3:
                raise OSError("download failed")
            return super().read(min(size, 3))

    monkeypatch.setattr(
        urllib.request.OpenerDirector, "open", lambda self, request: Response(b"abcdef")
    )
    image = ImageMetadata(
        "test:1", "qcow2", "https://example.invalid/image", hashlib.sha256(b"abcdef").hexdigest()
    )
    monkeypatch.setattr(ImageCatalog, "bundled", lambda: ImageCatalog([image]))
    app = Application(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
    )
    out = StringIO() if mode == "pipe" else Tty()
    err = StringIO()
    flags = {"json": ["--json"], "quiet": ["--quiet"], "debug": ["--debug"], "verbose": ["-v"]}.get(
        mode, []
    )
    status = main(["image", "pull", "test:1", *flags], application=app, stdout=out, stderr=err)
    assert status == (6 if failure else 0), err.getvalue()
    text = out.getvalue()
    if mode in ("tty", "verbose"):
        assert "3 bytes" in text
        assert "Pulling test:1" in text
        assert "\r\033[2K" in text
        if length == "6":
            assert "50%" in text
            assert "[" in text
        else:
            assert "total unknown" in text
            assert "%" not in text
        if failure:
            assert text.endswith("\r\033[2K")
            assert "download failed" in err.getvalue()
        else:
            assert text.index("\r\033[2K") < text.index("id: test:1")
    else:
        assert "\r" not in text and "\033" not in text
        assert "bytes" not in text
    assert not list((tmp_path / "cache/images/tmp").iterdir())
    if failure:
        assert not list((tmp_path / "cache/images/refs").iterdir())
        assert not list((tmp_path / "cache/images/blobs/sha256").iterdir())


@pytest.mark.parametrize("failure", ["interrupt", "checksum"])
def test_progress_cleans_up_on_interrupt_or_verification_failure(tmp_path, monkeypatch, failure):
    import urllib.request

    class Response(BytesIO):
        @property
        def headers(self):
            return {"Content-Length": "6"}

        def read(self, size=-1):
            if failure == "interrupt":
                raise KeyboardInterrupt
            return super().read(size)

    monkeypatch.setattr(
        urllib.request.OpenerDirector, "open", lambda self, request: Response(b"abcdef")
    )
    image = ImageMetadata("test:1", "qcow2", "https://example.invalid/image", "0" * 64)
    monkeypatch.setattr(ImageCatalog, "bundled", lambda: ImageCatalog([image]))
    app = Application(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
    )
    out, err = Tty(), StringIO()
    if failure == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            main(["image", "pull", "test:1"], application=app, stdout=out, stderr=err)
    else:
        assert main(["image", "pull", "test:1"], application=app, stdout=out, stderr=err) == 6
        assert "checksum mismatch" in err.getvalue()
    assert out.getvalue().endswith("\r\033[2K")
    assert not list((tmp_path / "cache/images/tmp").iterdir())
    assert not list((tmp_path / "cache/images/refs").iterdir())
    assert not list((tmp_path / "cache/images/blobs/sha256").iterdir())
