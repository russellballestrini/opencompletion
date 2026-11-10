"""What a walk of our running app found (2026-11-10), each as a probe in
headless Chromium (tests/functional/browser.py) that fails without its fix:

- a guest's "Your rooms" row opened an empty ⋯ menu & threw;
- a guest's sidebar kept a room's slug after the room was titled;
- follow-up chips looked like plain grey text, between a reply & its actions;
- a table on a phone broke its words letter by letter instead of scrolling;
- home & chat said "People only" while our model list loaded, so a quick
  Send started a silent room;
- our Rooms page said "Yours 0" to a guest whose sidebar listed rooms;
- verse & addresses lost their line breaks.

Run: make test-ui
"""

import json
import re

import pytest

from browser import run_probe  # tests/functional/browser.py
from test_ui_contract import make_room, make_user, sign_in

pytestmark = pytest.mark.functional


@pytest.fixture
def client(test_app):
    return test_app.test_client()


def passed(out):
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


def before_script(html, script_name, stub):
    """`html` with `stub` (a <script>) run just before one of our scripts:
    a probe appended to the page comes too late to stand in for what that
    script asks our server as it loads."""
    marker = f'<script src="/static/js/{script_name}'
    assert marker in html, script_name
    return html.replace(marker, stub + marker, 1)


# Every probe: `check`, a place for uncaught errors, & a wait that costs
# the page's virtual clock a few milliseconds.
PROBE_HEAD = r"""
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    window.addEventListener('error', e => problems.push('uncaught: ' + e.message));
    window.onunhandledrejection = e => problems.push('unhandled: ' + e.reason);
    const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
    const finish = () => { document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed'; };
"""


# --- 1. A guest's ⋯ menu -------------------------------------------------

GUEST_ACTIONS_PROBE = (
    r"""
<script>
window.addEventListener('load', async () => {"""
    + PROBE_HEAD
    + r"""
    const calls = [];
    window.fetch = async (url, options) => {
        calls.push([url, options && options.method, options && options.body]);
        return new Response(JSON.stringify({title: 'Far away'}), {status: 200, headers: {'Content-Type': 'application/json'}});
    };
    window.prompt = () => 'Far away';

    // Someone else's room offers a guest nothing: no ⋯ at all.
    const theirs = document.querySelector('#public-chats li[data-room-name="adas-room"]');
    check(theirs && !theirs.querySelector('[data-chat-actions]'), 'no ⋯ on a room that offers nothing');
    const lobbyId = document.querySelector('#public-chats li[data-room-name="lobby"]').dataset.roomId;

    // This browser's history: a room our server lists on this page, one
    // recorded with what our server said, an old entry with no id & a
    // room only its owner may rename.
    localStorage.setItem('recentChats', JSON.stringify([
        {name: 'lobby', title: null, public: true},
        {name: 'elsewhere', title: 'Elsewhere', public: true, id: 999, anyoneMayRetitle: true},
        {name: 'legacy', title: null, public: true},
        {name: 'owned', title: null, public: true, id: 998, anyoneMayRetitle: false},
    ]));
    fillGuestChats(null);
    const row = name => document.querySelector(`#your-chats li[data-room-name="${name}"]`);
    const dots = name => row(name).querySelector('[data-chat-actions]');
    check(dots('lobby') && row('lobby').dataset.roomId === lobbyId, 'a row takes its id from our server\'s own row');
    check(dots('elsewhere') && row('elsewhere').dataset.roomId === '999', 'a recorded room carries its id');
    check(!dots('legacy'), 'no id to ask our server about: no ⋯');
    check(!dots('owned'), 'only its owner may rename it: no ⋯');

    dots('elsewhere').click();
    let menu = document.querySelector('.chat-actions-menu');
    const labels = menu ? [...menu.querySelectorAll('button')].map(b => b.textContent).join() : null;
    check(labels === 'Rename,Refresh title', 'a guest is offered what anyone may do: ' + labels);
    check(menu && document.activeElement === menu.querySelector('button'), 'focus moves into the menu');
    if (menu) [...menu.querySelectorAll('button')].find(b => b.textContent === 'Rename').click();
    await wait(20);
    const post = calls.find(([url, method]) => url === '/api/rooms/999/title' && method === 'POST');
    check(post && JSON.parse(post[2]).name === 'elsewhere', 'rename names the room it means: ' + JSON.stringify(calls));
    check(row('elsewhere').querySelector('.chat-list-name').textContent === 'Far away', 'the row takes its new title');

    // A ⋯ that offers nothing (should one ever be drawn) opens no menu.
    const stray = document.createElement('button');
    stray.dataset.chatActions = '';
    row('legacy').appendChild(stray);
    stray.click();
    check(!document.querySelector('.chat-actions-menu') && stray.hidden, 'never an empty menu');

    // What gets recorded: a room just made, as our server described it.
    rememberRecentChat('fresh-room', null, {id: 5, anyoneMayRetitle: true});
    const fresh = recentChats()[0];
    check(fresh.name === 'fresh-room' && fresh.id === 5 && fresh.anyoneMayRetitle === true, 'a new room is recorded with its id: ' + JSON.stringify(fresh));
    rememberRecentChat('fresh-room', 'Fresh');
    check(recentChats()[0].id === 5 && recentChats()[0].title === 'Fresh', 'a later title keeps the id');
    finish();
});
</script>
"""
)


@pytest.mark.parametrize("width", [375, 1280])
def test_a_guests_row_offers_what_anyone_may_do_or_no_menu(client, tmp_path, width):
    ada = make_user("ada@example.test", "ada")
    make_room("lobby", message="hi")
    make_room("adas-room", owner=ada, message="hi")
    html = client.get("/").get_data(as_text=True)
    passed(run_probe(html, GUEST_ACTIONS_PROBE, tmp_path, width))


def test_a_chat_page_says_what_its_room_offers_a_guest(client):
    ada = make_user("ada@example.test", "ada")
    lobby = make_room("lobby")
    make_room("adas-room", owner=ada)
    html = client.get("/chat/lobby").get_data(as_text=True)
    assert f"roomId: {lobby.id}," in html
    assert "anyoneMayRetitle: true" in html
    theirs = client.get("/chat/adas-room").get_data(as_text=True)
    assert "anyoneMayRetitle: false" in theirs


def test_a_new_room_says_whether_anyone_may_retitle_it(client):
    made = client.post("/api/rooms/create", json={"name": "open-room"}).get_json()
    assert made["room"]["anyone_may_retitle"] is True
    sign_in(client, make_user("ada@example.test", "ada"))
    mine = client.post("/api/rooms/create", json={"name": "adas-own"}).get_json()
    assert mine["room"]["anyone_may_retitle"] is False


def test_a_remembered_id_never_renames_another_room(client):
    """A browser's recorded id may outlive its room (SQLite reuses ids):
    the name sent along must still be that room's."""
    from models import Room, db

    lobby = make_room("lobby")
    wrong = client.post(
        f"/api/rooms/{lobby.id}/title", json={"title": "Mine now", "name": "garden"}
    )
    assert wrong.status_code == 404
    assert db.session.get(Room, lobby.id).title is None
    right = client.post(
        f"/api/rooms/{lobby.id}/title", json={"title": "Lobby talk", "name": "lobby"}
    )
    assert right.status_code == 200
    assert db.session.get(Room, lobby.id).title == "Lobby talk"
