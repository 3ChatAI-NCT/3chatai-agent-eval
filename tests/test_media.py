from contextlib import contextmanager

import httpx
import pytest
from fastapi import HTTPException

from agent_eval import media


def test_image_proxy_requires_an_explicit_host_allowlist(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("An unconfigured image source must not reach the network")

    monkeypatch.setattr(media.httpx, "stream", unexpected)
    with pytest.raises(HTTPException) as caught:
        media.load_image({"url": "https://assets.example.com/x.png"})
    assert caught.value.status_code == 422


@pytest.mark.parametrize("url", [
    "http://assets.example.com/x.png", "https://127.0.0.1/x.png",
    "https://gateway.example.test/x.png", "https://assets.example.com.evil.example/x.png",
    "https://user:secret@assets.example.com/x.png",
    "https://assets.example.com:8080/x.png", "https://assets.example.com:bad/x.png",
    "https://media.example.com.evil.example/x.png",
])
def test_image_proxy_rejects_non_media_sources_before_network(url, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Rejected URLs must never reach the network")

    monkeypatch.setattr(media.httpx, "stream", unexpected)
    with pytest.raises(HTTPException) as caught:
        media.load_image({"url": url}, "assets.example.com,media.example.com")
    assert caught.value.status_code == 422


@pytest.mark.parametrize("content_type, status, payload, expected", [
    ("image/png", 200, b"png", 200),
    ("image/svg+xml", 200, b"<svg/>", 422),
    ("text/html", 200, b"<html/>", 422),
    ("image/png", 404, b"", 502),
    ("image/png", 302, b"", 502),
    ("image/png", 200, b"oversized-image", 413),
])
def test_image_proxy_validates_response_type_size_and_status(
    content_type, status, payload, expected, monkeypatch,
):
    @contextmanager
    def stream(method, url, **kwargs):
        assert kwargs["follow_redirects"] is False
        yield httpx.Response(status, content=payload, headers={"Content-Type": content_type},
                             request=httpx.Request(method, url))

    monkeypatch.setattr(media.httpx, "stream", stream)
    monkeypatch.setattr(media, "MAX_IMAGE_BYTES", 10)
    if expected == 200:
        assert media.load_image({"url": "https://assets.example.com/x.png"},
                                "assets.example.com") == (
            payload, content_type,
        )
    else:
        with pytest.raises(HTTPException) as caught:
            media.load_image({"url": "https://assets.example.com/x.png"}, "assets.example.com")
        assert caught.value.status_code == expected


def test_image_proxy_accepts_explicitly_configured_media(monkeypatch):
    url = "https://media.example.com/customer.png"

    @contextmanager
    def stream(method, source, **kwargs):
        assert source == url
        assert kwargs["follow_redirects"] is False
        yield httpx.Response(200, content=b"png", headers={"Content-Type": "image/png"},
                             request=httpx.Request(method, source))

    monkeypatch.setattr(media.httpx, "stream", stream)
    assert media.load_image({"url": url}, "assets.example.com, media.example.com") == (
        b"png", "image/png")
