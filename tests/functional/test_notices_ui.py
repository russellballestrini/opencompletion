"""Messages we never store (someone joined, an activity step, a reply's
"Processing..." placeholder) each carry an id of their own
(activity_utils.notice_id). With no id they all became #message-null &
the first reply to finish cleared every one of them, a model's words
included. Run: make test-ui"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from browser import run_probe  # noqa: E402  tests/functional/browser.py

pytestmark = pytest.mark.functional

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(test_app):
    from models import Room, db

    db.session.add(Room(name="lobby"))
    db.session.commit()
    return test_app.test_client()


def test_no_message_is_sent_without_an_id_of_its_own():
    for name in ("app.py", "activity.py"):
        source = (ROOT / name).read_text()
        assert '"id": None' not in source, name
        assert '"tmp-1"' not in source, name


def test_notice_ids_are_their_own():
    from activity_utils import notice_id

    ids = {notice_id() for _ in range(200)}
    assert len(ids) == 200 and all(re.fullmatch(r"notice-[0-9a-f]{12}", i) for i in ids)
    assert notice_id("processing").startswith("processing-")


def test_joining_and_replying_send_notices_with_their_own_ids(test_app, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module.gevent, "spawn", lambda *a, **k: None)
    socket = app_module.socketio.test_client(
        app_module.app, flask_test_client=test_app.test_client()
    )
    socket.emit("join", {"room_name": "lobby", "username": "ada"})
    socket.emit(
        "chat_message",
        {"room_name": "lobby", "username": "ada", "message": "hi", "model": "qwen"},
    )
    ids = [
        e["args"][0]["id"] for e in socket.get_received() if e["name"] == "chat_message"
    ]
    notices = [i for i in ids if isinstance(i, str)]
    assert any(i.startswith("notice-") for i in notices)  # "ada has joined"
    assert any(i.startswith("processing-") for i in notices)  # the placeholder
    assert None not in ids and len(set(notices)) == len(notices)


NOTICES_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const deliver = (event, data) => testSocket.deliver(event, data);
    deliver('chat_message', {id: 'notice-aaaaaaaaaaaa', content: 'ada has joined the room.'});
    deliver('chat_message', {id: 'notice-bbbbbbbbbbbb', username: 'System', content: 'I cannot use that tool here.'});
    deliver('chat_message', {id: 'processing-111111111111', content: 'Processing...'});
    deliver('chat_message', {id: 'processing-222222222222', content: 'Processing...'});
    check(document.querySelectorAll('#chat .message-wrapper').length === 4, 'each notice is its own element');
    check(!document.querySelector('#message-notice-bbbbbbbbbbbb .button-container'), 'a notice has no Delete or Edit');
    // One reply ends: its placeholder (the oldest) goes, nothing else does.
    deliver('delete_processing_message', 7);
    check(!document.getElementById('message-processing-111111111111'), 'the finished reply’s placeholder goes');
    check(document.getElementById('message-processing-222222222222'), 'another reply’s placeholder stays');
    check(document.getElementById('message-notice-aaaaaaaaaaaa') && document.getElementById('message-notice-bbbbbbbbbbbb'), 'notices stay');
    deliver('delete_processing_message', 8);
    check(!document.querySelector('#chat [id^="message-processing-"]'), 'the second reply clears its own');
    check(document.getElementById('message-notice-bbbbbbbbbbbb'), 'a model’s notice survives every reply ending');
    // A stored message still gets its actions.
    deliver('chat_message', {id: 42, username: 'ada', content: 'hello'});
    check(document.querySelector('#message-42 .button-container'), 'stored messages keep Delete & Edit');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


def test_a_finished_reply_clears_only_its_own_placeholder(client, tmp_path):
    html = client.get("/chat/lobby").get_data(as_text=True)
    out = run_probe(html, NOTICES_PROBE, tmp_path, 1280)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)
