"""Fixtures several integration test modules share. pytest hands them over
by name, so no module imports a fixture from another (pyflakes reads that
as a redefinition, F811). A module's own fixture of the same name wins."""

from types import SimpleNamespace

import pytest


@pytest.fixture
def harness(test_app, monkeypatch):
    """chat_gpt with a scripted model (test_tools_chat's Model) & every
    emit collected."""
    import app as app_module

    from tests.integration.test_tools_chat import Model

    model = Model()
    client = SimpleNamespace(chat=SimpleNamespace(completions=model))
    monkeypatch.setattr(
        app_module, "get_openai_client_and_model", lambda name: (client, name)
    )
    emitted = []
    monkeypatch.setattr(
        app_module.socketio, "emit", lambda *a, **k: emitted.append((a, k))
    )
    monkeypatch.setattr(app_module.socketio, "sleep", lambda s: None)
    monkeypatch.setattr(app_module, "title_untitled_room", lambda name: None)
    return SimpleNamespace(app=app_module, model=model, emitted=emitted)


@pytest.fixture
def quiet(monkeypatch):
    """No reply is spawned & qwen is our only system user."""
    import app as app_module
    from routes import DEPS

    monkeypatch.setattr(app_module.gevent, "spawn", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "SYSTEM_USERS", ["qwen"])
    monkeypatch.setitem(DEPS, "system_users", ["qwen"])
