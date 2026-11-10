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


# /models: our model list at once, from the map we have; a stale map is
# refreshed behind the answer, one refresh at a time (a walk of our app,
# 2026-11-10: with two endpoints down every page waited 1.5 s for its
# picker, & home said "People only" meanwhile).


class _Refresh:
    """What gevent.spawn hands back, without running anything: a test
    runs the refresh itself, when it means to."""

    def __init__(self, run):
        self.run = run
        self.done = False

    def ready(self):
        return self.done

    def finish(self):
        self.run()
        self.done = True


@pytest.fixture
def spawned(monkeypatch):
    refreshes = []

    def spawn(run, *args):
        refreshes.append(_Refresh(run))
        return refreshes[-1]

    monkeypatch.setattr(app_module.gevent, "spawn", spawn)
    monkeypatch.setattr(app_module, "_model_map_refresh", None)
    return refreshes


def listed():
    return app_module.app.test_client().get("/models").get_json()["models"]


def test_models_answers_from_a_fresh_map_without_asking_anyone(
    endpoints, monkeypatch, spawned
):
    a = fake_client(A, ["qwen"])
    load(monkeypatch, {A: a})
    a.models = SimpleNamespace(list=lambda: pytest.fail("a fresh map was re-listed"))
    assert listed() == ["qwen"]
    assert spawned == []


def test_a_stale_map_answers_at_once_and_refreshes_behind(
    endpoints, monkeypatch, spawned
):
    a = fake_client(A, ["qwen"])
    load(monkeypatch, {A: a})
    a.models = SimpleNamespace(
        list=lambda: SimpleNamespace(
            data=[SimpleNamespace(id="qwen"), SimpleNamespace(id="hermes")]
        )
    )
    stale = app_module.time.time() - app_module.ENDPOINT_HEALTH_TTL - 1
    monkeypatch.setattr(app_module, "_model_map_refreshed_at", stale)
    # The answer is what we had: nobody waited on an endpoint for it.
    assert listed() == ["qwen"]
    assert len(spawned) == 1
    # One refresh at a time, however many pages ask meanwhile.
    assert listed() == ["qwen"]
    assert len(spawned) == 1
    spawned[0].finish()
    assert listed() == ["qwen", "hermes"]
    assert len(spawned) == 1  # fresh again: nothing more to do
    # Stale once more: a finished refresh never blocks the next one.
    monkeypatch.setattr(app_module, "_model_map_refreshed_at", stale)
    listed()
    assert len(spawned) == 2


def test_a_map_never_built_is_built_while_we_wait(endpoints, monkeypatch, spawned):
    monkeypatch.setattr(app_module, "_model_map_refreshed_at", 0.0)
    a = fake_client(A, ["qwen"])
    monkeypatch.setattr(app_module, "ENDPOINTS", [{"base_url": A, "api_key": "k"}])
    monkeypatch.setattr(app_module, "get_client_for_endpoint", lambda url, key: a)
    assert listed() == ["qwen"]
    assert spawned == []


def test_a_refresh_that_fails_keeps_our_map_and_waits_a_ttl(
    endpoints, monkeypatch, spawned
):
    load(monkeypatch, {A: fake_client(A, ["qwen"])})
    monkeypatch.setattr(app_module, "_model_map_refreshed_at", 1.0)

    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(app_module, "initialize_model_map", broken)
    listed()
    spawned[0].finish()
    assert listed() == ["qwen"]
    assert len(spawned) == 1


def test_nobody_reads_a_half_built_map(endpoints, monkeypatch):
    """Listing waits on the network & other greenlets run meanwhile: our
    map keeps its old models until the new one is whole."""
    a, b = fake_client(A, ["qwen"]), fake_client(B, ["hermes"])
    load(monkeypatch, {A: a, B: b})
    seen = []

    def slow_list():
        seen.append(sorted(app_module.MODEL_CLIENT_MAP))
        return SimpleNamespace(data=[SimpleNamespace(id="hermes")])

    b.models = SimpleNamespace(list=slow_list)
    app_module.initialize_model_map()
    assert seen == [["hermes", "qwen"]]
    assert sorted(app_module.MODEL_CLIENT_MAP) == ["hermes", "qwen"]
