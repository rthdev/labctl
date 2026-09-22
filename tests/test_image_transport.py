import hashlib
import os
import urllib.request
from email.message import Message
from io import BytesIO
from urllib.response import addinfourl

import pytest

from labctl.images import ImageError, ImageMetadata, ImageStore


def response(payload=b"image", *, location=None):
    headers = Message()
    if location:
        headers["Location"] = location
    headers["Content-Length"] = str(len(payload))
    result = addinfourl(
        BytesIO(payload), headers, "https://images.example/image", code=302 if location else 200
    )
    result.msg = "Found" if location else "OK"
    return result


@pytest.fixture
def proxy_environment(monkeypatch):
    for key in os.environ:
        if key.lower().endswith("_proxy"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(urllib.request, "_opener", None)


@pytest.mark.parametrize("variable", ["https_proxy", "HTTPS_PROXY"])
@pytest.mark.parametrize("bypass_variable", ["no_proxy", "NO_PROXY"])
@pytest.mark.parametrize("bypass", ["", "images.example", ".example", "*"])
def test_pull_uses_urllib_environment_proxy_semantics(
    tmp_path, monkeypatch, proxy_environment, variable, bypass_variable, bypass
):
    monkeypatch.setenv(variable, "http://proxy.example:3128")
    monkeypatch.setenv(bypass_variable, bypass)
    requests = []

    def https_open(self, request):
        requests.append((request.host, request._tunnel_host))
        return response()

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    image = ImageMetadata(
        "test:1", "qcow2", "https://images.example/image", hashlib.sha256(b"image").hexdigest()
    )
    assert ImageStore(tmp_path).pull(image).read_bytes() == b"image"
    assert requests == (
        [("images.example", None)] if bypass else [("proxy.example:3128", "images.example")]
    )


def test_pull_refuses_https_redirect_downgrade(tmp_path, monkeypatch, proxy_environment):
    monkeypatch.setattr(
        urllib.request.HTTPSHandler,
        "https_open",
        lambda self, request: response(location="http://images.example/image"),
    )
    insecure = []

    def http_open(self, request):
        insecure.append(request.full_url)
        return response()

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", http_open)
    image = ImageMetadata(
        "test:1", "qcow2", "https://images.example/image", hashlib.sha256(b"image").hexdigest()
    )
    store = ImageStore(tmp_path)
    with pytest.raises(ImageError, match="HTTPS"):
        store.pull(image)
    assert insecure == []
    assert not list(store.tmp.iterdir())
    assert not list(store.refs.iterdir())


@pytest.mark.parametrize("corrupt", [False, True])
def test_https_redirect_keeps_proxy_bypass_and_digest_checks(
    tmp_path, monkeypatch, proxy_environment, corrupt
):
    monkeypatch.setenv("HTTPS_PROXY", "http://ignored.example:9000")
    monkeypatch.setenv("https_proxy", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "mirror.example")
    seen = []

    def https_open(self, request):
        seen.append((request.host, request._tunnel_host))
        if request.full_url == "https://images.example/image":
            return response(location="https://mirror.example/image")
        return response(b"corrupt" if corrupt else b"image")

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    image = ImageMetadata(
        "test:1", "qcow2", "https://images.example/image", hashlib.sha256(b"image").hexdigest()
    )
    store = ImageStore(tmp_path)
    if corrupt:
        with pytest.raises(ImageError, match="checksum mismatch"):
            store.pull(image)
        assert not list(store.refs.iterdir())
        assert not list(store.blobs.iterdir())
    else:
        store.pull(image)
        assert store.resolve(image.id)[2] is True
    assert seen == [("proxy.example:3128", "images.example"), ("mirror.example", None)]
    assert not list(store.tmp.iterdir())
