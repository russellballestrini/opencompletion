"""A vision model's external image is fetched through our proxy with the
image's whole address as ONE parameter, the way static/js/chat/2-vision.js
sends it."""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import app as app_module


@pytest.fixture
def proxy(monkeypatch):
    """Our proxy, answered in-process; every request it saw."""
    seen = []
    answer = {"status": 200, "type": "image/png", "body": b"\x89PNG"}

    def handler(request):
        seen.append(request)
        return httpx.Response(
            answer["status"],
            headers={"content-type": answer["type"]},
            content=answer["body"],
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(app_module, "_external_image_cache", {})
    return seen, answer


def test_an_address_with_its_own_query_arrives_whole(proxy):
    seen, _ = proxy
    image = "https://cdn.example/a.png?w=640&h=480&sig=a+b%2Fc#top"

    data_url = app_module.fetch_external_image_as_base64(image)

    assert data_url == "data:image/png;base64,iVBORw=="
    query = parse_qs(urlsplit(str(seen[0].url)).query, strict_parsing=True)
    assert query == {"uri_target": [image]}
    assert seen[0].url.fragment == ""


def test_a_fetched_image_is_asked_for_once(proxy):
    seen, _ = proxy
    image = "https://cdn.example/a.png"

    first = app_module.fetch_external_image_as_base64(image)

    assert app_module.fetch_external_image_as_base64(image) == first
    assert len(seen) == 1


def test_a_page_that_is_not_an_image_is_refused(proxy):
    _, answer = proxy
    answer["type"] = "text/html; charset=utf-8"

    assert app_module.fetch_external_image_as_base64("https://a.example/") is None


def test_a_robots_refusal_is_no_image(proxy):
    _, answer = proxy
    answer["status"] = 403

    assert app_module.fetch_external_image_as_base64("https://a.example/x") is None


def test_a_proxy_failure_is_no_image(proxy):
    _, answer = proxy
    answer["status"] = 502

    assert app_module.fetch_external_image_as_base64("https://a.example/x") is None
