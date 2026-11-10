"""Undo for deletes in headless Chromium: a deleted message leaves one
quiet line in its place (who wrote it, who deleted it, Restore), Restore
asks the server, & the restored message lands between its neighbours.
Run: make test-ui"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from browser import run_probe  # noqa: E402  tests/functional/browser.py

pytestmark = pytest.mark.functional


@pytest.fixture
def client(test_app):
    from models import Room, db

    db.session.add(Room(name="lobby"))
    db.session.commit()
    return test_app.test_client()


UNDO_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const order = () => [...document.getElementById('chat').children]
        .filter((el) => /^(message|deleted)-\d+$/.test(el.id)).map((el) => el.id).join(' ');

    testSocket.deliver('previous_messages', {id: 1, username: 'ada', content: 'first', versions: 1});
    testSocket.deliver('previous_messages', {id: 2, username: 'ada', content: 'second <img src=x onerror="window.pwned=1">', versions: 1});
    testSocket.deliver('previous_messages', {id: 3, username: 'qwen', content: 'third', from_model: true, versions: 1});
    // Someone joining later: a line for a message deleted before they came,
    // placed by its id though it arrives after our history
    testSocket.deliver('message_deleted', {message_id: 0, username: 'carol', deleted_by: null, restorable: true});
    check(order() === 'deleted-0 message-1 message-2 message-3', 'a late line sits where its id belongs (' + order() + ')');
    check(document.getElementById('deleted-0').textContent.startsWith('A message from carol was deleted.'), 'with nobody to name, it names nobody');

    // Deleted while we watch
    testSocket.deliver('message_deleted', {message_id: 2, username: 'ada', deleted_by: 'bob <b>', restorable: true});
    check(order() === 'deleted-0 message-1 deleted-2 message-3', 'the line takes the message\'s place (' + order() + ')');
    const line = document.getElementById('deleted-2');
    check(line && line.querySelector('span').textContent === 'A message from ada was deleted by bob <b>.', 'says who wrote it & who deleted it, as text (' + (line && line.textContent) + ')');
    check(line && !line.querySelector('b') && !line.textContent.includes('second'), 'its words are gone from our page');
    const restore = line && line.querySelector('button');
    check(restore && restore.textContent === 'Restore' && restore.type === 'button', 'offers Restore, a button');
    check(restore && restore.tabIndex === 0 && !restore.disabled, 'reachable by keyboard');
    if (restore) {
        restore.focus();
        check(document.activeElement === restore, 'takes focus');
        const box = restore.getBoundingClientRect();
        if (window.innerWidth <= 768) check(box.height >= 40 && box.width >= 40, 'a 40px tap target on phones (' + box.width + 'x' + box.height + ')');
        const quiet = getComputedStyle(line);
        check(parseFloat(quiet.fontSize) <= 13, 'a quiet line (' + quiet.fontSize + ')');
        restore.click();
    }
    const asked = testSocket.sent.filter(([event]) => event === 'restore_message');
    check(asked.length === 1 && asked[0][1].message_id === 2 && asked[0][1].room_name === 'lobby', 'Restore asks the server (' + JSON.stringify(asked) + ')');
    check(order() === 'deleted-0 message-1 deleted-2 message-3', 'nothing moves until the server says so');

    // A message that came after the delete, then the restore itself
    testSocket.deliver('chat_message', {id: 4, username: 'ada', content: 'fourth'});
    testSocket.deliver('message_restored', {id: 2, username: 'ada', content: 'second <img src=x onerror="window.pwned=1">', versions: 1});
    check(order() === 'deleted-0 message-1 message-2 message-3 message-4', 'the restored message lands between its neighbours (' + order() + ')');
    const back = document.getElementById('message-2');
    check(back && back.textContent.includes('second') && back.classList.contains('is-person'), 'with its words & its look');
    check(back && back.querySelector('.button-container') && !window.pwned && !back.querySelector('[onerror]'), 'its actions, sanitised');
    testSocket.deliver('message_restored', {id: 2, username: 'ada', content: 'second', versions: 1});
    check(document.querySelectorAll('#message-2').length === 1, 'restored once');

    // Restoring the oldest: before everything
    testSocket.deliver('message_restored', {id: 0, username: 'carol', content: 'zeroth', versions: 1});
    check(order() === 'message-0 message-1 message-2 message-3 message-4', 'an oldest message returns to the top (' + order() + ')');

    // No Restore window: the message just goes
    testSocket.deliver('message_deleted', {message_id: 4, username: 'ada', deleted_by: 'bob', restorable: false});
    check(order() === 'message-0 message-1 message-2 message-3', 'with no window nothing is offered (' + order() + ')');

    // An Edit & resend that hid a branch takes its deleted lines along
    testSocket.deliver('message_deleted', {message_id: 3, username: 'qwen', deleted_by: 'bob', restorable: true});
    testSocket.deliver('messages_hidden', {ids: [1, 2, 3], revision_id: 9});
    check(order() === 'message-0', 'a replaced branch takes its deleted lines along (' + order() + ')');

    check(document.documentElement.scrollWidth <= window.innerWidth, 'no sideways scroll');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [375, 1280])
def test_a_deleted_message_leaves_a_line_that_restores_it(client, tmp_path, width):
    html = client.get("/chat/lobby").get_data(as_text=True)
    out = run_probe(html, UNDO_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


def test_the_deleted_line_is_styled_from_our_tokens():
    css = (Path(__file__).resolve().parents[2] / "static/css/style.css").read_text()
    rule = css[css.index(".deleted-line {") : css.index(".room-stats {")]
    assert "var(--text-secondary)" in rule and "var(--border-color)" in rule
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(", rule.replace("#chat", ""))
