"""app.sees_images: a model whose name says nothing is asked once, with a
red square, whether it reads images."""

import base64
from types import SimpleNamespace

import openai
import pytest

import app as app_module


@pytest.fixture
def ask(monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_VISION_PROBE", "on")
    monkeypatch.setattr(app_module, "SEES_IMAGES", {})
    asked = []
    answers = {}

    def create(client, **kwargs):
        asked.append(kwargs)
        answer = answers[kwargs["model"]]
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=answer))]
        )

    monkeypatch.setattr(app_module, "create_completion_skip_thinking", create)
    monkeypatch.setattr(
        app_module, "get_openai_client_and_model", lambda name=None: (object(), name)
    )
    return asked, answers


def test_the_red_square_is_a_real_png():
    head, data = app_module.RED_SQUARE.split(",", 1)
    assert head == "data:image/png;base64"
    assert base64.b64decode(data).startswith(b"\x89PNG\r\n\x1a\n")


def test_a_model_that_names_red_sees_and_is_asked_once(ask):
    asked, answers = ask
    answers["plain-name"] = "<think>hm</think>Red."
    assert app_module.sees_images("plain-name")
    assert app_module.sees_images("plain-name")
    assert len(asked) == 1
    part = asked[0]["messages"][0]["content"][1]
    assert part["image_url"]["url"] == app_module.RED_SQUARE
    # & from then on it is a vision model everywhere (describe, history)
    assert app_module.is_vision_model("plain-name")


def test_a_wrong_colour_or_a_refusal_is_its_answer(ask):
    asked, answers = ask
    answers["andrea"] = "I like food"
    response = SimpleNamespace(status_code=400, headers={}, request=None)
    answers["text-only"] = openai.BadRequestError(
        "image input not supported", response=response, body=None
    )
    for model in ("andrea", "text-only"):
        assert not app_module.sees_images(model)
        assert not app_module.sees_images(model)
        assert not app_module.is_vision_model(model)
    assert len(asked) == 2


def test_a_failure_that_is_no_answer_is_asked_again_later(ask, monkeypatch):
    asked, answers = ask
    answers["flaky"] = TimeoutError("slow")
    assert not app_module.sees_images("flaky")
    assert not app_module.sees_images("flaky")  # not again at once
    assert len(asked) == 1
    monkeypatch.setattr(app_module, "SEES_IMAGES_RETRY_S", 0)
    answers["flaky"] = "red"
    assert app_module.sees_images("flaky")


def test_off_never_asks(ask, monkeypatch):
    asked, answers = ask
    monkeypatch.setenv("OPENCOMPLETION_VISION_PROBE", "off")
    assert not app_module.sees_images("anything")
    assert asked == []


def test_a_room_with_an_image_hands_it_to_a_model_that_sees(test_app, ask):
    from models import Message, Room, db

    asked, answers = ask
    answers["plain-name"] = "red"
    with test_app.app_context():
        room = Room(name="sees-room")
        db.session.add(room)
        db.session.commit()
        db.session.add(
            Message(
                username="ada",
                content=f'look <img src="{app_module.RED_SQUARE}">',
                room_id=room.id,
            )
        )
        db.session.commit()
    _, history = app_module.reply_history("sees-room", "plain-name")
    assert len(asked) == 1
    assert any(isinstance(turn["content"], list) for turn in history)

    answers["plain-name-2"] = "blue"
    _, text_only = app_module.reply_history("sees-room", "plain-name-2")
    assert not any(isinstance(turn["content"], list) for turn in text_only)


def test_qwen3_point_releases_see_by_name(ask):
    asked, _ = ask
    assert app_module.is_vision_model("turboderp/Qwen3.8-27B-exl3")
    assert app_module.is_vision_model("Qwen/Qwen3.5-35B-A3B")
    assert not app_module.is_vision_model("Qwen/Qwen3-8B")
    assert asked == []
