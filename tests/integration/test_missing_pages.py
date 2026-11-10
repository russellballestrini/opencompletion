"""A missing page, a name no room may have & someone's solo chat answer in
our shell (themes, a phone's viewport, a way on), never a bare line of
text; our API keeps its own short replies."""

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def http(test_app):
    return test_app.test_client()


def in_our_shell(response):
    html = response.get_data(as_text=True)
    return 'name="viewport"' in html and "style.css" in html and 'href="/"' in html


def test_a_name_no_room_may_have_is_a_page_and_makes_no_room(http):
    from models import Room

    response = http.get("/chat/NOT_valid!")
    assert response.status_code == 404
    assert in_our_shell(response) and b"No such room" in response.data
    assert Room.query.count() == 0


def test_an_address_with_no_page_is_a_page(http):
    response = http.get("/nope")
    assert response.status_code == 404
    assert in_our_shell(response) and b"Nothing here" in response.data


def test_someones_solo_chat_says_so_without_showing_it(http):
    from models import Message, Room, db

    room = Room(name="diary")
    room.is_private, room.owner_id = True, 4242
    db.session.add(room)
    db.session.commit()
    db.session.add(Message(username="ada", content="dear diary", room_id=room.id))
    db.session.commit()

    response = http.get("/chat/diary")
    assert response.status_code == 403
    assert in_our_shell(response) and b"solo chat" in response.data
    assert b"dear diary" not in response.data


def test_our_api_keeps_its_short_replies(http):
    response = http.get("/api/nope")
    assert response.status_code == 404
    assert 'name="viewport"' not in response.get_data(as_text=True)
    assert http.get("/api/messages/99999/versions").json == {
        "error": "Message not found"
    }
