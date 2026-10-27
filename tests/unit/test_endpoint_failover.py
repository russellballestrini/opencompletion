"""A model id is pinned to the lowest MODEL_ENDPOINT_n listing it & fails
over to the other endpoints listing it, before any other model answers."""

from types import SimpleNamespace

import httpx
import openai
import pytest

import app as app_module

A, B, C = "http://a.test/v1", "http://b.test/v1", "http://c.test/v1"


def fake_client(url, models=(), fail=None):
    """A client whose models.list() names `models` & whose completions
    raise `fail` (an exception) or return a stream tagged with `url`."""
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if fail:
            raise fail
        return f"stream from {url}"

    listing = SimpleNamespace(data=[SimpleNamespace(id=m) for m in models])
    client = SimpleNamespace(
        base_url=url + "/",
        calls=calls,
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        models=SimpleNamespace(list=lambda: listing),
    )
    client.with_options = lambda **kw: client
    return client


def unreachable(url):
    return openai.APIConnectionError(request=httpx.Request("POST", url))


@pytest.fixture
def endpoints(monkeypatch):
    """Endpoints A, B & C; `down` holds the URLs failing health checks."""
    down = set()
    monkeypatch.setattr(app_module, "MODEL_CLIENT_MAP", {})
    monkeypatch.setattr(app_module, "MODEL_ENDPOINTS", {})
    monkeypatch.setattr(app_module, "_endpoint_models", {})
    monkeypatch.setattr(app_module, "_endpoint_health", {})
    monkeypatch.setattr(
        app_module, "endpoint_is_healthy", lambda client, url: url not in down
    )
    return down


def load(monkeypatch, clients):
    """Point our model map at `clients` ({url: client}) & build it."""
    monkeypatch.setattr(
        app_module,
        "ENDPOINTS",
        [{"base_url": url, "api_key": "k"} for url in clients],
    )
    monkeypatch.setattr(
        app_module, "get_client_for_endpoint", lambda url, key: clients[url]
    )
    app_module.initialize_model_map()


def test_the_lowest_endpoint_is_the_pin_and_the_rest_back_it_up(endpoints, monkeypatch):
    a, b = fake_client(A, ["qwen", "hermes"]), fake_client(B, ["qwen"])
    load(monkeypatch, {A: a, B: b})
    assert app_module.MODEL_CLIENT_MAP["qwen"] == (a, A)
    assert app_module.MODEL_ENDPOINTS["qwen"] == [(a, A), (b, B)]
    assert app_module.get_openai_client_and_model("qwen") == (a, "qwen")


def test_a_pin_that_is_down_fails_over_to_the_same_model(endpoints, monkeypatch):
    a, b = fake_client(A, ["qwen", "hermes"]), fake_client(B, ["qwen"])
    load(monkeypatch, {A: a, B: b})
    endpoints.add(A)
    assert app_module.get_openai_client_and_model("qwen") == (b, "qwen")


def test_only_with_every_copy_down_does_another_model_answer(endpoints, monkeypatch):
    a, b = fake_client(A, ["qwen"]), fake_client(B, ["qwen"])
    c = fake_client(C, ["hermes"])
    load(monkeypatch, {A: a, B: b, C: c})
    endpoints.update({A, B})
    monkeypatch.setattr(app_module, "DEFAULT_MODEL", "MODEL_9")
    monkeypatch.setattr(app_module, "CONFIGURED_MODEL_NUMS", ["9"])
    monkeypatch.setattr(
        app_module, "_resolve_model_num", lambda num, check_health=True: (c, "hermes")
    )
    assert app_module.get_openai_client_and_model("qwen") == (c, "hermes")


def test_a_model_n_reference_that_is_down_finds_its_model_elsewhere(
    endpoints, monkeypatch
):
    a, b = fake_client(A, ["qwen"]), fake_client(B, ["qwen"])
    c = fake_client(C, ["hermes"])
    load(monkeypatch, {A: a, C: c, B: b})
    monkeypatch.setenv("MODEL_ENDPOINT_1", A)
    monkeypatch.setenv("MODEL_API_KEY_1", "k")
    monkeypatch.delenv("MODEL_NAME_1", raising=False)
    monkeypatch.setattr(app_module, "CONFIGURED_MODEL_NUMS", ["1", "2"])
    monkeypatch.setattr(app_module, "get_client_for_endpoint", lambda url, key: a)
    endpoints.add(A)
    # MODEL_2 (hermes) is up, but qwen on B comes before another model.
    assert app_module.get_openai_client_and_model("MODEL_1") == (b, "qwen")


def test_a_stream_that_fails_to_open_moves_to_the_next_copy(endpoints, monkeypatch):
    a = fake_client(A, ["qwen"], fail=unreachable(A))
    b = fake_client(B, ["qwen"])
    load(monkeypatch, {A: a, B: b})
    marked = []
    monkeypatch.setattr(app_module, "mark_endpoint_down", marked.append)

    stream = app_module.open_with_failover(
        a, "qwen", lambda client: client.chat.completions.create(model="qwen")
    )
    assert stream == f"stream from {B}"
    assert marked == [A]


def test_our_own_bad_request_is_not_retried_elsewhere(endpoints, monkeypatch):
    request = httpx.Request("POST", A)
    bad = openai.BadRequestError(
        "bad", response=httpx.Response(400, request=request), body=None
    )
    a, b = fake_client(A, ["qwen"], fail=bad), fake_client(B, ["qwen"])
    load(monkeypatch, {A: a, B: b})

    with pytest.raises(openai.BadRequestError):
        app_module.open_with_failover(
            a, "qwen", lambda client: client.chat.completions.create(model="qwen")
        )
    assert b.calls == []


def test_the_last_copy_failing_surfaces_its_error(endpoints, monkeypatch):
    a = fake_client(A, ["qwen"], fail=unreachable(A))
    b = fake_client(B, ["qwen"], fail=unreachable(B))
    load(monkeypatch, {A: a, B: b})

    with pytest.raises(openai.APIConnectionError):
        app_module.open_with_failover(
            a, "qwen", lambda client: client.chat.completions.create(model="qwen")
        )
    assert len(a.calls) == len(b.calls) == 1
