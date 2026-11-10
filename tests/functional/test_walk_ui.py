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


# --- 2. A title reaches every row ---------------------------------------

GUEST_TITLE_PROBE = (
    r"""
<script>
window.addEventListener('load', async () => {"""
    + PROBE_HEAD
    + r"""
    // A guest whose rows come from this browser alone (no id on them).
    document.querySelectorAll('#public-chats li').forEach(row => row.remove());
    localStorage.setItem('recentChats', JSON.stringify([
        {name: 'lobby', title: null, public: true},
        {name: 'elsewhere', title: null, public: true},
    ]));
    fillGuestChats('lobby');
    const row = name => document.querySelector(`#your-chats li[data-room-name="${name}"]`);
    check(row('lobby') && !row('lobby').dataset.roomId, 'a guest row without an id');

    testSocket.deliver('update_room_title', {title: 'Weekend <b>plans</b>'});
    check(document.getElementById('chat-title').textContent === 'Weekend <b>plans</b>', 'top bar shows the title');
    const name = row('lobby').querySelector('.chat-list-name');
    check(name.textContent === 'Weekend <b>plans</b>' && !name.querySelector('b'), 'our open room\'s row shows it too, as text: ' + name.textContent);
    check(row('lobby').querySelector('a').title === 'Weekend <b>plans</b>', 'its link is titled alike');

    // Another room of ours is retitled elsewhere: found by name.
    testSocket.deliver('update_room_list', {id: 77, name: 'elsewhere', title: 'Far away'});
    check(row('elsewhere').querySelector('.chat-list-name').textContent === 'Far away', 'a row with no id is renamed by its room\'s name');
    const kept = recentChats();
    check(kept.find(c => c.name === 'elsewhere').title === 'Far away', 'this browser keeps the new title');
    check(kept[0].name === 'lobby', 'a retitled room does not jump our list: ' + kept.map(c => c.name));
    // A row our server rendered (with an id) is renamed all the same.
    const served = row('elsewhere').cloneNode(true);
    served.dataset.roomId = '77';
    document.getElementById('public-chats').appendChild(served);
    testSocket.deliver('update_room_list', {id: 77, name: 'elsewhere', title: 'Further still'});
    check(served.querySelector('.chat-list-name').textContent === 'Further still', 'every row of a room is renamed');
    finish();
});
</script>
"""
)


def test_a_title_reaches_a_guests_rows(client, tmp_path):
    make_room("lobby", message="hi")
    html = client.get("/chat/lobby").get_data(as_text=True)
    passed(run_probe(html, GUEST_TITLE_PROBE, tmp_path, 1280))


# --- 3. Follow-up chips ---------------------------------------------------

CHIPS_PROBE = (
    r"""
<script>
window.addEventListener('load', async () => {"""
    + PROBE_HEAD
    + r"""
    testSocket.deliver('chat_message', {id: 5, username: 'hermes', content: 'Kermit, Piggy & Gonzo.', from_model: true});
    testSocket.deliver('followup_suggestions', {message_id: 5, suggestions: ['Who made the Muppets?', 'Which one is the oldest?']});
    const wrapper = document.getElementById('message-5');
    const chips = [...wrapper.querySelectorAll('.followup-chip')];
    check(chips.length === 2, 'two chips: ' + chips.length);
    const look = getComputedStyle(chips[0]);
    check(look.borderTopStyle === 'solid' && look.borderTopWidth === '1px', 'a chip is outlined: ' + look.borderTopStyle + ' ' + look.borderTopWidth);
    check(look.backgroundColor !== 'rgba(0, 0, 0, 0)', 'a chip has its own surface: ' + look.backgroundColor);
    const words = getComputedStyle(wrapper.querySelector('.message-content')).color;
    check(look.color === words, 'a chip reads in our text colour, not a quiet grey: ' + look.color);
    check(parseFloat(look.borderTopLeftRadius) >= 16, 'a chip is a pill: ' + look.borderTopLeftRadius);
    const phone = window.innerWidth <= 768;
    chips.forEach(chip => {
        const height = chip.getBoundingClientRect().height;
        check(height >= (phone ? 40 : 36), 'a chip is tall enough to tap: ' + height);
    });
    // A reply, then its own actions, then what to ask next.
    const content = wrapper.querySelector('.message-content').getBoundingClientRect();
    const actions = wrapper.querySelector(':scope > .button-container').getBoundingClientRect();
    const row = wrapper.querySelector('.followups').getBoundingClientRect();
    check(content.bottom <= actions.top + 1, 'actions come after the reply');
    check(actions.bottom <= row.top + 1, 'chips come after the reply\'s actions: ' + actions.bottom + ' vs ' + row.top);
    check(document.documentElement.scrollWidth <= window.innerWidth + 1, 'the page does not scroll sideways');
    // A chip still sends its question.
    chips[0].click();
    const sent = testSocket.sent.find(([event]) => event === 'chat_message');
    check(sent && sent[1].message === 'Who made the Muppets?', 'a chip sends its question');
    finish();
});
</script>
"""
)


@pytest.mark.parametrize("width", [375, 1280])
def test_followup_chips_look_like_chips_after_a_replys_actions(client, tmp_path, width):
    make_room("lobby")
    html = client.get("/chat/lobby").get_data(as_text=True)
    passed(run_probe(html, CHIPS_PROBE, tmp_path, width))


# --- 4. Tables on a phone -----------------------------------------------

TABLE_PROBE = (
    r"""
<script>
window.addEventListener('load', async () => {"""
    + PROBE_HEAD
    + r"""
    const table = [
        '| Name | Diameter (km) | Day length | Moons | Discovered | Notes |',
        '|---|---|---|---|---|---|',
        '| Mercury | 4,879 | 1,408 hours | 0 | Antiquity | Smallest planet |',
        '| Venus | 12,104 | 5,832 hours | 0 | Antiquity | Hottest planet |',
        '| Neptune | 49,244 | 16 hours | 16 | 1846 | Farthest planet |',
    ].join('\n');
    testSocket.deliver('chat_message', {id: 6, username: 'hermes', content: 'Planets:\n\n' + table, from_model: true});
    testSocket.deliver('chat_message', {id: 7, username: 'ada', content: table});
    for (const id of [6, 7]) {
        const el = document.querySelector(`#message-${id} table`);
        check(el, 'a table renders in message ' + id);
        if (!el) continue;
        // One word, one line: never "Na/m/e" or "M/er/cu/ry".
        for (const cell of el.querySelectorAll('th, td')) {
            const word = cell.textContent.trim();
            if (/\s/.test(word)) continue;
            const range = document.createRange();
            range.selectNodeContents(cell);
            const lines = new Set([...range.getClientRects()].map(r => Math.round(r.top)));
            check(lines.size === 1, `"${word}" stays whole in message ${id}: ${lines.size} lines`);
        }
        check(getComputedStyle(el).overflowX === 'auto' && el.scrollWidth > el.clientWidth, `a wide table scrolls inside message ${id}: ${el.scrollWidth} in ${el.clientWidth}`);
        const column = document.getElementById('message-' + id).getBoundingClientRect();
        check(el.getBoundingClientRect().right <= column.right + 1, 'the table stays inside its message ' + id);
    }
    const chat = document.getElementById('chat');
    check(chat.scrollWidth <= chat.clientWidth + 1, 'our chat does not scroll sideways');
    check(document.documentElement.scrollWidth <= window.innerWidth + 1, 'the page does not scroll sideways');
    // Words outside a table still break when they must.
    testSocket.deliver('chat_message', {id: 8, username: 'hermes', content: 'x'.repeat(400), from_model: true});
    check(document.documentElement.scrollWidth <= window.innerWidth + 1, 'a long word still wraps');
    finish();
});
</script>
"""
)


@pytest.mark.parametrize("width", [320, 390])
def test_a_wide_table_scrolls_and_keeps_its_words_whole(client, tmp_path, width):
    make_room("lobby")
    html = client.get("/chat/lobby").get_data(as_text=True)
    passed(run_probe(html, TABLE_PROBE, tmp_path, width))


# --- 5. The model picker while its list loads -----------------------------


# Our server's /models, answered when a probe says so (`modelsReply.send`)
# or never. MODELS_WAIT_MS (4 s) is past the page's virtual-time budget:
# that one wait is shortened, every other timer keeps its time.
def models_stub(saved_model=None):
    saved = (
        f"localStorage.setItem('selectedModel', {json.dumps(saved_model)});"
        if saved_model
        else "localStorage.removeItem('selectedModel');"
    )
    return (
        """<script>
localStorage.removeItem('modelList');
localStorage.removeItem('modelListExpiration');
"""
        + saved
        + """
window.modelsReply = {};
window.modelsReply.arrived = new Promise(resolve => {
    window.modelsReply.send = models => resolve({ok: true, json: () => Promise.resolve({models})});
});
window.fetch = url => String(url) === '/models'
    ? window.modelsReply.arrived
    : Promise.reject(new TypeError('offline test page'));
const realSetTimeout = window.setTimeout;
window.setTimeout = (fn, ms, ...rest) => realSetTimeout(fn, ms >= 4000 ? 40 : ms, ...rest);
</script>"""
    )


HOME_MODELS_HEAD = (
    r"""
<script>
window.addEventListener('load', async () => {"""
    + PROBE_HEAD
    + r"""
    const created = [];
    const models = window.modelsReply;
    window.fetch = (url, options) => {
        if (String(url) === '/models') return models.arrived;
        if (String(url) === '/api/rooms/create') {
            const body = JSON.parse(options.body);
            created.push(body);
            return Promise.resolve({ok: true, json: () => Promise.resolve({room: {id: 9, name: body.name, anyone_may_retitle: true}})});
        }
        return Promise.reject(new TypeError('offline'));
    };
    let opened = null;
    window.startChatNavigate = url => { opened = url; };
    const input = document.getElementById('composer-input');
    const picker = document.getElementById('composer-model');
    const send = document.getElementById('composer-send');
    const shown = () => picker.options[picker.selectedIndex].textContent;
    const pending = () => JSON.parse(sessionStorage.getItem('pendingMessage') || 'null');
    sessionStorage.removeItem('pendingMessage');

    check(shown() === 'Loading models…' && picker.disabled, 'the picker says our list is loading: ' + shown());
    check(shown() !== 'People only (no model)', 'it never claims people only meanwhile');
    input.value = 'Why is the sky blue?';
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
"""
)

HOME_LIST_ARRIVES_PROBE = HOME_MODELS_HEAD + r"""
    await wait(20);
    check(created.length === 0 && !opened, 'Send waits for our model list: ' + created.length);
    check(send.disabled, 'Send shows it is busy');
    models.send(['hermes', 'qwen']);
    await wait(60);
    check(created.length === 1, 'one room once the list is in: ' + created.length);
    check(pending() && pending().model === 'hermes', 'a first visit gets a model that answers: ' + JSON.stringify(pending()));
    check(!picker.disabled && shown() === 'hermes', 'the picker shows it: ' + shown());
    check(![...picker.options].some(o => o.textContent === 'Loading models…'), 'the loading line is gone');
    check(recentChats()[0] && recentChats()[0].id === 9 && recentChats()[0].anyoneMayRetitle === true, 'the new room is recorded with its id');
    finish();
});
</script>
"""

HOME_SAVED_MODEL_PROBE = HOME_MODELS_HEAD + r"""
    await wait(200);
    check(created.length === 1, 'a list that never comes still sends: ' + created.length);
    check(pending() && pending().model === 'qwen', 'with the model picked before: ' + JSON.stringify(pending()));
    check(localStorage.getItem('selectedModel') === 'qwen', 'our saved model is kept');
    finish();
});
</script>
"""

HOME_NO_MODEL_PROBE = HOME_MODELS_HEAD + r"""
    await wait(200);
    check(created.length === 0 && !opened, 'no silent room is started: ' + created.length);
    check(/model list has not loaded/.test(document.getElementById('composer-error').textContent), 'it says why: ' + document.getElementById('composer-error').textContent);
    check(!send.disabled && input.value === 'Why is the sky blue?', 'our words wait, Send works again');
    // The list arrives: now it sends.
    models.send(['hermes']);
    await wait(20);
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    await wait(60);
    check(created.length === 1 && pending() && pending().model === 'hermes', 'then it sends with a model: ' + JSON.stringify(pending()));
    finish();
});
</script>
"""


@pytest.mark.parametrize(
    "probe,saved",
    [
        (HOME_LIST_ARRIVES_PROBE, None),
        (HOME_SAVED_MODEL_PROBE, "qwen"),
        (HOME_NO_MODEL_PROBE, None),
    ],
    ids=["list-arrives", "saved-model", "no-model"],
)
def test_home_waits_for_its_model_list(client, tmp_path, probe, saved):
    html = before_script(
        client.get("/").get_data(as_text=True), "home.js", models_stub(saved)
    )
    passed(run_probe(html, probe, tmp_path, 1280))


def test_home_with_no_model_list_says_people_only(client, tmp_path):
    """Our server did not answer /models at all (every probe's default):
    the picker stops loading & says what is true."""
    probe = (
        r"""
<script>
window.addEventListener('load', async () => {"""
        + PROBE_HEAD
        + r"""
    const picker = document.getElementById('composer-model');
    check(!picker.disabled && picker.options[picker.selectedIndex].textContent === 'People only (no model)', 'people only, said plainly');
    check(picker.options.length === 1, 'no loading line left');
    finish();
});
</script>
"""
    )
    passed(run_probe(client.get("/").get_data(as_text=True), probe, tmp_path, 1280))


CHAT_MODELS_HEAD = (
    r"""
<script>
window.addEventListener('load', async () => {"""
    + PROBE_HEAD
    + r"""
    const models = window.modelsReply;
    const picker = document.getElementById('model-select');
    const box = document.getElementById('message');
    const shown = () => picker.options[picker.selectedIndex].textContent;
    const said = () => testSocket.sent.filter(([event]) => event === 'chat_message').map(([, data]) => data);
    check(shown() === 'Loading models…' && picker.disabled, 'the picker says our list is loading: ' + shown());
    box.value = 'Why is the sky blue?';
    document.getElementById('send-button').click();
    box.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    await wait(20);
"""
)

CHAT_LIST_ARRIVES_PROBE = CHAT_MODELS_HEAD + r"""
    check(said().length === 0, 'Send waits for our model list');
    check(box.value === 'Why is the sky blue?', 'our words stay in the box meanwhile');
    models.send(['hermes', 'qwen']);
    await wait(60);
    check(said().length === 1, 'sent once, when the list is in: ' + said().length);
    check(said()[0] && said()[0].model === 'qwen' && said()[0].message === 'Why is the sky blue?', 'with our saved model: ' + JSON.stringify(said()[0]));
    check(!picker.disabled && shown() === 'qwen' && picker.options[0].value === 'None' && picker.options[0].textContent === 'People only (no model)', 'the picker is itself again: ' + shown());
    check(box.value === '', 'the box is cleared once sent');
    finish();
});
</script>
"""

CHAT_NO_MODEL_PROBE = CHAT_MODELS_HEAD + r"""
    await wait(200);
    check(said().length === 0, 'nothing is sent to nobody');
    check(box.value === 'Why is the sky blue?', 'our words are kept');
    check(/model list has not loaded/.test(document.querySelector('.composer-status').textContent), 'it says why');
    finish();
});
</script>
"""


@pytest.mark.parametrize(
    "probe,saved",
    [(CHAT_LIST_ARRIVES_PROBE, "qwen"), (CHAT_NO_MODEL_PROBE, None)],
    ids=["list-arrives", "no-model"],
)
def test_chat_waits_for_its_model_list(client, tmp_path, probe, saved):
    make_room("lobby")
    html = before_script(
        client.get("/chat/lobby").get_data(as_text=True),
        "chat/3-room.js",
        models_stub(saved),
    )
    passed(run_probe(html, probe, tmp_path, 1280))
