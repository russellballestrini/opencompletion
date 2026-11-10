"""Our chat page & its room, against what a walk of the running app found
(fox, 2026-11-10): a page that never joined its room. Run: make test-ui"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from browser import run_probe  # noqa: E402  tests/functional/browser.py

pytestmark = pytest.mark.functional


@pytest.fixture
def page(test_app):
    from models import Room, db

    db.session.add(Room(name="lobby"))
    db.session.commit()
    return test_app.test_client().get("/chat/lobby").get_data(as_text=True)


def probe(body):
    """A probe: `body` runs once our page has loaded, with check() &
    deliver(); it may await."""
    return (
        r"""
<script>
window.addEventListener('load', async () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const deliver = (event, data) => testSocket.deliver(event, data);
    const sent = (name) => testSocket.sent.filter(([event]) => event === name);
    const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
    try {
"""
        + body
        + r"""
    } catch (error) {
        problems.push('threw: ' + (error.stack || error));
    }
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""
    )


def passed(out):
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


def with_socket(html, lands):
    """Our page with its (fake) socket's connection landing as `lands`
    says: see FAKE_SOCKET in browser.py."""
    return html.replace(
        "<head>", f"<head><script>window.TEST_SOCKET_LANDS = '{lands}';</script>", 1
    )


JOINED_PROBE = probe(r"""
    const joins = sent('join');
    check(testSocket.connected, 'our socket connected');
    check(joins.length === 1, 'joined its room once, not ' + joins.length + ' times');
    check(joins[0] && joins[0][1].room_name === 'lobby' && joins[0][1].username, 'the join names our room & who we are');
""")


def test_a_connection_that_lands_at_once_still_joins(page, tmp_path):
    """17 of 40 loads never joined: the socket was made (& connecting) in
    our page's head & its connection could land before 3-room.js attached
    the handler that joins. Now it connects once every script has loaded."""
    passed(run_probe(with_socket(page, "at once"), JOINED_PROBE, tmp_path, 1280))


def test_a_socket_already_connected_joins_all_the_same(page, tmp_path):
    """Belt & braces: connected before any handler was attached (whatever
    autoConnect said), our page still joins, once."""
    passed(run_probe(with_socket(page, "early"), JOINED_PROBE, tmp_path, 1280))


REJOIN_PROBE = probe(r"""
    check(sent('join').length === 0, 'no join before a connection lands');
    testSocket.land();
    check(sent('join').length === 1, 'joined when its connection landed');
    startChat();
    testSocket.deliver('connect');
    check(sent('join').length === 1, 'one join per connection, however often asked: ' + sent('join').length);
    testSocket.drop();
    testSocket.land();
    check(sent('join').length === 2, 'rejoined on reconnect: ' + sent('join').length);
""")


def test_reconnecting_rejoins(page, tmp_path):
    passed(run_probe(page, REJOIN_PROBE, tmp_path, 1280))
