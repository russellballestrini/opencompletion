"""An unsent message outlives a refresh, one draft per place: home & each
room under its own localStorage key, never crossing (fox, 2026-11-02)."""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from browser import run_probe  # noqa: E402  tests/functional/browser.py


@pytest.fixture
def client(test_app):
    from models import Room, db

    db.session.add(Room(name="lobby"))
    db.session.commit()
    return test_app.test_client()


CHAT_DRAFT_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    localStorage.setItem('draft:home', 'home words');
    localStorage.setItem('draft:room:elsewhere', 'another room');
    const box = document.getElementById('message');
    box.value = 'half a thought';
    box.dispatchEvent(new Event('input'));
    check(localStorage.getItem('draft:room:lobby') === 'half a thought', 'typing keeps this room\'s draft');
    check(localStorage.getItem('draft:home') === 'home words' && localStorage.getItem('draft:room:elsewhere') === 'another room', 'other places\' drafts are untouched');
    // Sent: gone, & leaving the page doesn't keep it.
    sendMessage();
    window.dispatchEvent(new Event('pagehide'));
    check(localStorage.getItem('draft:room:lobby') === null, 'a sent message is no draft');
    check(testSocket.sent.some(([e, d]) => e === 'chat_message' && d.message === 'half a thought'), 'it was sent');
    box.value = '   ';
    box.dispatchEvent(new Event('input'));
    check(localStorage.getItem('draft:room:lobby') === null, 'blank is no draft');
    // A refresh: a new box for a room gets its own draft (last: these boxes
    // keep their text & would save it as the page goes).
    localStorage.setItem('draft:room:lobby', 'half a thought');
    const again = document.createElement('textarea');
    keepDraft(again, 'room:lobby');
    check(again.value === 'half a thought', 'a refresh brings this room\'s draft back');
    const other = document.createElement('textarea');
    keepDraft(other, 'room:elsewhere');
    check(other.value === 'another room', 'each room gets only its own draft');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


HOME_DRAFT_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    localStorage.setItem('draft:room:lobby', 'a room\'s words');
    const box = document.getElementById('composer-input');
    box.value = 'ask about PETG';
    box.dispatchEvent(new Event('input'));
    window.dispatchEvent(new Event('pagehide'));
    check(localStorage.getItem('draft:home') === 'ask about PETG', 'home keeps its draft as it goes');
    check(localStorage.getItem('draft:room:lobby') === 'a room\'s words', 'a room\'s draft is untouched');
    homeDraft.clear();  // what starting a chat does
    window.dispatchEvent(new Event('pagehide'));
    check(localStorage.getItem('draft:home') === null, 'a started chat leaves no home draft');
    // A refresh brings a draft back (last: the box would save as the page goes).
    localStorage.setItem('draft:home', 'ask about PETG');
    const again = document.createElement('textarea');
    keepDraft(again, 'home');
    check(again.value === 'ask about PETG', 'a refresh brings home\'s draft back');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


def test_a_rooms_draft_outlives_a_refresh_and_stays_its_own(client, tmp_path):
    html = client.get("/chat/lobby").get_data(as_text=True)
    out = run_probe(html, CHAT_DRAFT_PROBE, tmp_path, 1280)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


def test_homes_draft_outlives_a_refresh_and_stays_its_own(client, tmp_path):
    html = client.get("/").get_data(as_text=True)
    out = run_probe(html, HOME_DRAFT_PROBE, tmp_path, 1280)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)
