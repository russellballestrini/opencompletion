"""Editing a message in headless Chromium: the editor opens one line tall,
grows with its text to 14 lines (then scrolls) & keeps its Save & Cancel
in view. Run: make test-ui"""

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


EDIT_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    for (let i = 0; i < 30; i++) {
        testSocket.deliver('chat_message', {id: 100 + i, username: 'bob', content: 'filler ' + i});
    }
    testSocket.deliver('chat_message', {id: 1, username: 'ada', content: 'one line'});
    document.querySelector('#message-1 .edit-button').click();
    const editor = document.querySelector('#message-1 textarea.message-edit');
    const line = parseFloat(getComputedStyle(editor).lineHeight);
    const lines = () => Math.round((editor.clientHeight - parseFloat(getComputedStyle(editor).paddingTop) * 2) / line);
    check(editor && lines() === 1, 'opens one line tall (' + lines() + ')');
    check(document.activeElement === editor, 'the editor has focus');
    const type = (text) => { editor.value = text; editor.dispatchEvent(new Event('input')); };
    type('a\nb\nc\nd\ne');
    check(lines() === 5, 'grows to its text (' + lines() + ')');
    type(Array.from({length: 40}, (_, i) => 'line ' + i).join('\n'));
    check(lines() === 14, 'stops at 14 lines (' + lines() + ')');
    check(editor.scrollHeight > editor.clientHeight && getComputedStyle(editor).overflowY === 'auto', 'then scrolls');
    const save = document.querySelector('#message-1 .save-button').getBoundingClientRect();
    const chat = document.getElementById('chat').getBoundingClientRect();
    check(save.bottom <= chat.bottom + 1 && save.top >= chat.top - 1, 'Save stays in view');
    type('short again');
    check(lines() === 1, 'shrinks back (' + lines() + ')');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [375, 1280])
def test_the_editor_grows_with_its_text_and_keeps_its_buttons_in_view(
    client, tmp_path, width
):
    html = client.get("/chat/lobby").get_data(as_text=True)
    out = run_probe(html, EDIT_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)
