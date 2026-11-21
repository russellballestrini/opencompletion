"""A guest's name step on our chat page: asked in our page, never by a
browser prompt() (a blocking dialog on a person's first chat page, ugly on
phones & a hang for headless browsers). Our page joins its room only once
a name is chosen. Run: make test-ui"""

import json
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


@pytest.fixture
def page(client):
    return client.get("/chat/lobby").get_data(as_text=True)


def lands_at_once(html, before=""):
    """Our page with a (fake) socket that is connected the moment it is
    asked to connect, & `before` run ahead of every page script."""
    script = f"<script>window.TEST_SOCKET_LANDS = 'at once';{before}</script>"
    return html.replace("<head>", "<head>" + script, 1)


def probe(body):
    """`body` runs once our page has loaded, with check(), sent() & join()
    (types a name & submits our step as Enter or Join would)."""
    return (
        r"""
<script>
window.addEventListener('load', async () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const sent = (name) => testSocket.sent.filter(([event]) => event === name);
    const step = document.getElementById('name-step');
    const input = document.getElementById('guest-name');
    const composer = document.getElementById('message-form');
    const shown = (el) => !!el && getComputedStyle(el).display !== 'none';
    const join = (typed) => { input.value = typed; step.requestSubmit(); };
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


FIRST_VISIT_PROBE = probe(r"""
    check(shown(step), 'a first visit shows our name step');
    check(!shown(composer), 'in our composer\'s place');
    check(!shown(document.getElementById('activity-controls')), 'no activity is started before a name');
    check(document.activeElement === input, 'focus is in its field');
    const label = document.querySelector('label[for="guest-name"]');
    check(label && /call you/.test(label.textContent), 'its question labels the field');
    check(input.placeholder === 'guest' && input.maxLength === 50, 'placeholder guest, 50 characters');
    check(step.querySelector('button[type="submit"]').textContent.trim() === 'Join', 'one Join button');
    check(/^\/auth\?next=/.test(step.querySelector('a').getAttribute('href')), 'it links to sign in');
    check(!testSocket.connected && !sent('join').length, 'nothing joined before a name');
    check(username === null && localStorage.getItem('guestUsername') === null, 'no name yet');

    join(TYPED);
    const joins = sent('join');
    check(testSocket.connected && joins.length === 1, 'joined once, not ' + joins.length + ' times');
    check(joins[0] && joins[0][1].username === EXPECTED && joins[0][1].room_name === 'lobby', 'joined our room as ' + EXPECTED + ': ' + JSON.stringify(joins[0]));
    check(username === EXPECTED && localStorage.getItem('guestUsername') === EXPECTED, 'the name is kept in this browser');
    check(!shown(step) && shown(composer), 'our composer takes its place back');
    check(shown(document.getElementById('activity-controls')), 'activities are offered again');
    check(document.activeElement === document.getElementById('message'), 'focus moves to our message box');

    step.requestSubmit();
    check(sent('join').length === 1, 'a second submit joins nothing again');
""")


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("  Ada Lovelace  ", "Ada Lovelace"),
        ("", "guest"),
        ("   ", "guest"),
        ("x" * 80, "x" * 50),
    ],
)
def test_a_first_visit_asks_a_name_then_joins_under_it(page, tmp_path, typed, expected):
    body = FIRST_VISIT_PROBE.replace("TYPED", json.dumps(typed))
    body = body.replace("EXPECTED", json.dumps(expected))
    passed(run_probe(lands_at_once(page), body, tmp_path, 1280, guest_name=None))


STORED_PROBE = probe(r"""
    check(step && !shown(step), 'no name step for a guest we know');
    check(shown(composer), 'our composer is there');
    const joins = sent('join');
    check(joins.length === 1 && joins[0][1].username === 'ada', 'joined at once as ada: ' + JSON.stringify(joins));
    check(localStorage.getItem('guestUsername') === 'ada', 'their name stays');
""")


def test_a_stored_name_skips_the_step(page, tmp_path):
    passed(
        run_probe(lands_at_once(page), STORED_PROBE, tmp_path, 1280, guest_name="ada")
    )


SIGNED_IN_PROBE = probe(r"""
    check(!step, 'no name step on a signed-in page');
    check(shown(composer), 'our composer is there');
    const joins = sent('join');
    check(joins.length === 1 && joins[0][1].username === 'grace', 'joined at once as grace: ' + JSON.stringify(joins));
    check(localStorage.getItem('guestUsername') === null, 'a guest name left behind is cleared');
""")


def test_signed_in_people_never_see_it(client, tmp_path):
    from models import User, db

    user = User(email="grace@example.com", display_name="grace")
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as session:
        session["user_id"] = user.id
        session["user_email"] = user.email
        session["display_name"] = user.display_name
    html = client.get("/chat/lobby").get_data(as_text=True)
    assert 'id="name-step"' not in html
    # Whatever guest name this browser held before signing in is dropped.
    passed(
        run_probe(
            lands_at_once(html), SIGNED_IN_PROBE, tmp_path, 1280, guest_name="old"
        )
    )


PENDING_PROBE = probe(r"""
    const pending = () => sessionStorage.getItem('pendingMessage');
    check(shown(step), 'the name step comes first');
    check(pending() && !sent('chat_message').length, 'our first message waits, unsent');

    join('Ada');
    check(sent('join').length === 1, 'joined');
    check(pending() && !sent('chat_message').length, 'still waiting on our server\'s joined');
    testSocket.deliver('joined', {});
    const messages = sent('chat_message');
    check(messages.length === 1, 'sent once, not ' + messages.length + ' times');
    const message = messages[0] && messages[0][1];
    check(message && message.message === 'hello from home' && message.username === 'Ada', 'under the chosen name: ' + JSON.stringify(message));
    check(message && message.model === 'some-model' && message.room_name === 'lobby', 'with its model, to our room');
    check(pending() === null, 'handed over once');
""")


def test_a_message_from_home_is_sent_after_the_name_is_chosen(page, tmp_path):
    """Home & Activities' Play hand chat a first message (keepPendingMessage
    in utils.js): a guest with no name gets the step first, & the message
    is sent once they joined, never lost."""
    handed = json.dumps(
        json.dumps(
            {"room": "lobby", "message": "hello from home", "model": "some-model"}
        )
    )
    before = f"sessionStorage.setItem('pendingMessage', {handed});"
    html = lands_at_once(page, before)
    passed(run_probe(html, PENDING_PROBE, tmp_path, 1280, guest_name=None))


LAYOUT_PROBE = probe(r"""
    const phone = innerWidth <= 768;
    const box = (el) => el.getBoundingClientRect();
    const button = step.querySelector('button[type="submit"]');
    check(shown(step), 'our name step shows');
    check(document.documentElement.scrollWidth <= innerWidth, 'no sideways scroll (' + document.documentElement.scrollWidth + ' > ' + innerWidth + ')');
    const card = box(step);
    check(card.left >= 0 && card.right <= innerWidth, 'the card is on screen: ' + Math.round(card.left) + '-' + Math.round(card.right));
    check(card.bottom <= innerHeight, 'above the fold (' + Math.round(card.bottom) + ' of ' + innerHeight + ')');
    check(card.width <= 480, 'a small card, not a banner (' + Math.round(card.width) + ')');
    check(box(input).height >= 44 && box(button).height >= 44, 'field & Join are 44px tall: ' + box(input).height + ', ' + box(button).height);
    check(box(input).width >= 120, 'room to type a name (' + Math.round(box(input).width) + ')');
    check(Math.abs(box(input).top - box(button).top) <= 1, 'Join sits beside the field');
    check(box(button).right <= card.right && box(input).left >= card.left, 'both inside the card');
    if (phone) check(parseFloat(getComputedStyle(input).fontSize) >= 16, 'no zoom into the field on iOS');
    const hint = box(document.getElementById('name-step-hint'));
    check(hint.right <= card.right + 1 && hint.height > 0, 'its hint wraps inside the card');
""")


@pytest.mark.parametrize("width", [320, 375, 768, 1280])
def test_the_step_fits_every_screen(page, tmp_path, width):
    passed(run_probe(page, LAYOUT_PROBE, tmp_path, width, guest_name=None))


def test_chat_scripts_call_no_prompt_for_a_name():
    """The dialog is gone from our chat scripts, not only stubbed away."""
    for script in sorted((ROOT / "static/js/chat").glob("*.js")):
        assert "prompt(" not in script.read_text(), script.name
