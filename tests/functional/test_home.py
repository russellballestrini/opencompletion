"""Our home page: an empty chat whose sidebar lists your chats & public
ones, and whose first message makes a room (private or a chatroom) and
hands that message to the chat page it opens.

Server checks use Flask's test client; the composer runs in headless
Chromium (tests/functional/browser.py). Run: make test-ui
"""

import re
from unittest.mock import MagicMock

import pytest

from browser import run_probe  # tests/functional/browser.py
from test_ui_contract import make_room, make_user, sign_in

pytestmark = pytest.mark.functional


@pytest.fixture
def client(test_app):
    return test_app.test_client()


def sidebar(html, list_id):
    """The text of one sidebar list, by its heading's id."""
    section = re.search(
        rf'aria-labelledby="{list_id}"(.*?)</section>', html, re.S
    ).group(1)
    return section


def test_home_is_an_empty_chat(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="composer"' in html
    assert "Tabula rasa, in a beginning, where do we start?" in html
    assert 'data-mode="private"' in html and 'data-mode="chatroom"' in html
    assert "/static/js/home.js" in html


def test_guest_sees_public_chats_and_cannot_pick_private(client):
    make_room("lobby", message="hi")
    html = client.get("/").get_data(as_text=True)
    assert "lobby" in sidebar(html, "public-chats-heading")
    assert re.search(
        r'data-mode="private" aria-pressed="false" aria-disabled="true"', html
    )
    assert 'href="/auth?next=/"' in html


def test_your_chats_hold_your_rooms_and_never_anyone_elses_private_one(client):
    me = make_user("me@example.test", "me")
    other = make_user("other@example.test", "other")
    make_room("my-secret", owner=me, private=True)
    make_room("my-public", owner=me)
    make_room("their-secret", owner=other, private=True)
    sign_in(client, me)
    html = client.get("/").get_data(as_text=True)
    mine = sidebar(html, "your-chats-heading")
    assert "my-secret" in mine and "my-public" in mine
    assert "their-secret" not in html
    assert "my-secret" not in sidebar(html, "public-chats-heading")
    # Our user sits at the bottom of our sidebar, linking to settings.
    assert re.search(
        r'class="sidebar-user-link" href="/profile">.*?me</span>', html, re.S
    )


def test_archived_rooms_leave_our_sidebar(client):
    make_room("old-news", archived=True)
    assert "old-news" not in client.get("/").get_data(as_text=True)


def test_brand_comes_from_the_environment(client, monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_BRAND", "uncloseai")
    html = client.get("/").get_data(as_text=True)
    assert '<a class="brand" href="/">uncloseai</a>' in html
    assert "<title>uncloseai</title>" in html
    browse = client.get("/browse").get_data(as_text=True)
    assert '<a class="brand" href="/">uncloseai</a>' in browse


def test_join_says_joined_after_the_room_is_heard(test_app):
    """Our home page's first message waits for `joined`, sent once this
    client is in the Socket.IO room, so it hears its own message & reply."""
    import app as app_module

    socket = app_module.socketio.test_client(
        app_module.app, flask_test_client=test_app.test_client()
    )
    socket.emit("join", {"room_name": "fresh-room", "username": "ada"})
    names = [event["name"] for event in socket.get_received()]
    assert "joined" in names
    assert names.index("your_username") < names.index("joined")


COMPOSER_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const created = [];
    window.fetch = (url, options) => {
        if (String(url) === '/api/rooms/create') {
            const body = JSON.parse(options.body);
            created.push(body);
            return Promise.resolve({ok: true, json: () => Promise.resolve({room: {name: body.name}})});
        }
        return Promise.reject(new TypeError('offline'));
    };
    let opened = null;
    // Leaving the page would end the probe; record where we would go.
    window.startChatNavigate = url => { opened = url; };

    const input = document.getElementById('composer-input');
    check(getComputedStyle(input).display !== 'none', 'message box visible');
    check(document.querySelector('.mode-btn[data-mode="chatroom"]').getAttribute('aria-pressed') === 'true', 'guest starts a chatroom');
    document.querySelector('.mode-btn[data-mode="private"]').click();
    check(document.querySelector('.mode-btn[data-mode="chatroom"]').getAttribute('aria-pressed') === 'true', 'guest stays in chatroom mode');
    check(/Sign in/.test(document.getElementById('mode-hint').textContent), 'hint says why');

    input.value = 'Hello there, General Kenobi!';
    // A legacy, unmarked entry (maybe a private room) is never shown.
    localStorage.setItem('recentChats', JSON.stringify([{name: 'old-secret', title: 'Old secret'}]));
    check(recentChats().length === 0, 'unmarked history hidden');
    // Enter twice before the room exists still makes one chat.
    const enter = () => input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    enter();
    enter();
    const started = Date.now();
    const settle = () => {
        if (!opened && Date.now() - started < 1500) { setTimeout(settle, 20); return; }
        check(created.length === 1 && created[0].is_private === false, 'one public room created: ' + JSON.stringify(created));
        const name = created[0] && created[0].name;
        check(/^hello-there-general-kenobi-[a-z0-9]+$/.test(name || ''), 'readable room name: ' + name);
        check(opened === '/chat/' + name, 'opens the new room: ' + opened);
        const pending = JSON.parse(sessionStorage.getItem('pendingMessage') || 'null');
        check(pending && pending.room === name && pending.message === 'Hello there, General Kenobi!', 'first message handed over');
        check(recentChats().some(c => c.name === name), 'remembered under Your chats');
        forgetRecentChats();
        check(recentChats().length === 0, 'signing out forgets this browser\'s chats');
        check(takePendingMessage('another-room') === null, 'another room never takes it');
        check(takePendingMessage(name) && !sessionStorage.getItem('pendingMessage'), 'taken once');
        document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
    };
    setTimeout(settle, 20);
});
</script>
"""


def test_composer_starts_a_chat_and_hands_over_its_message(client, tmp_path):
    html = client.get("/").get_data(as_text=True)
    out = run_probe(html, COMPOSER_PROBE, tmp_path, 1280)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


DRAWER_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const shown = el => getComputedStyle(el).display !== 'none' && el.getBoundingClientRect().width > 0;
    const sidebar = document.getElementById('app-sidebar');
    const toggle = document.querySelector('.home-topbar .drawer-toggle');
    if (innerWidth <= 768) {
        check(shown(toggle) && !shown(sidebar), 'sidebar folds behind ☰');
        toggle.click();
        check(shown(sidebar) && sidebar.getBoundingClientRect().right <= innerWidth, 'drawer opens on screen');
        check(shown(sidebar.querySelector('.sidebar-user')), 'you, at the bottom of the drawer');
        document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));
        check(!shown(sidebar), 'Escape closes it');
    } else {
        check(!shown(toggle) && shown(sidebar), 'sidebar always shown on wide screens');
    }
    check(shown(document.getElementById('composer')), 'composer visible');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [320, 375, 768, 1280])
def test_home_sidebar_is_a_drawer_on_phones(client, tmp_path, width):
    make_room("lobby", message="hi")
    html = client.get("/").get_data(as_text=True)
    out = run_probe(html, DRAWER_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


PINNED_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    if (innerWidth <= 768) openDrawer('rooms');
    const user = document.querySelector('.sidebar-user');
    const brand = document.querySelector('.app-sidebar .brand');
    const list = document.querySelector('.sidebar-scroll');
    const onScreen = el => { const r = el.getBoundingClientRect(); return r.height > 0 && r.top >= 0 && r.bottom <= innerHeight + 1; };
    check(list.scrollHeight > list.clientHeight, 'many chats overflow into their own scroll');
    check(onScreen(user), 'you stay pinned bottom left: ' + user.getBoundingClientRect().bottom + ' > ' + innerHeight);
    list.scrollTop = list.scrollHeight;
    window.scrollTo(0, document.body.scrollHeight);
    check(onScreen(user) && onScreen(brand), 'scrolling our chats moves neither you nor our name');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [320, 1280])
def test_you_stay_pinned_bottom_left_with_many_chats(client, tmp_path, width):
    me = make_user("me@example.test", "me")
    for n in range(40):
        make_room(f"room-{n}", owner=me if n % 2 else None, private=n % 4 == 1)
    sign_in(client, me)
    html = client.get("/").get_data(as_text=True)
    out = run_probe(html, PINNED_PROBE, tmp_path, width, height=700)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


def test_a_new_chat_is_titled_after_its_first_reply(test_app, monkeypatch):
    """Our sidebars show a generated title, not a room's slug, as soon as
    a model has answered once; a titled room keeps its title."""
    import app as app_module
    from models import Room, db

    room = make_room("how-do-lifetimes-work-x1y2z3", message="How do lifetimes work?")
    emitted = []
    monkeypatch.setattr(
        app_module, "gpt_generate_room_title", lambda messages: " Rust lifetimes "
    )
    monkeypatch.setattr(
        app_module.socketio, "emit", lambda event, data, **kw: emitted.append(event)
    )
    app_module.title_untitled_room(room.name)
    db.session.expire_all()  # titled in its own app context
    assert Room.query.filter_by(name=room.name).one().title == "Rust lifetimes"
    assert "update_room_title" in emitted and "update_room_list" in emitted

    monkeypatch.setattr(
        app_module, "gpt_generate_room_title", lambda messages: "Something else"
    )
    app_module.title_untitled_room(room.name)
    db.session.expire_all()  # titled in its own app context
    assert Room.query.filter_by(name=room.name).one().title == "Rust lifetimes"


def test_no_model_leaves_a_chat_untitled(test_app, monkeypatch):
    import app as app_module
    from models import Room, db

    room = make_room("quiet-room", message="hello")
    monkeypatch.setattr(app_module, "gpt_generate_room_title", lambda messages: None)
    app_module.title_untitled_room(room.name)
    db.session.expire_all()  # titled in its own app context
    assert Room.query.filter_by(name="quiet-room").one().title is None


def test_sidebar_shows_titles_over_names(client):
    make_room("lobby-abc123")
    from models import Room, db

    Room.query.filter_by(name="lobby-abc123").one().title = "Weekend plans"
    db.session.commit()
    public = sidebar(client.get("/").get_data(as_text=True), "public-chats-heading")
    assert "Weekend plans" in public and ">lobby-abc123<" not in public


def test_title_prompt_puts_its_one_system_message_first(test_app, monkeypatch):
    """Qwen-style chat templates reject a system message anywhere but first
    ("System message must be at the beginning"); a model's own replies in
    our history must never become system turns."""
    import app as app_module
    from models import Message

    room = make_room("titled-room")
    from models import db

    for who, text in [
        ("ada", "How do lifetimes work?"),
        ("hermes", "They bound refs."),
    ]:
        db.session.add(Message(username=who, content=text, room_id=room.id))
    db.session.commit()
    monkeypatch.setattr(app_module, "SYSTEM_USERS", ["hermes"])

    sent = {}

    def fake_completion(client, **kwargs):
        sent.update(kwargs)
        reply = MagicMock()
        reply.choices[0].message.content = "Rust lifetimes"
        return reply

    monkeypatch.setattr(app_module, "create_completion_skip_thinking", fake_completion)
    monkeypatch.setattr(
        app_module, "get_openai_client_and_model", lambda: (object(), "model")
    )
    messages = (
        Message.query.filter_by(room_id=room.id).order_by(Message.id.desc()).all()
    )
    assert app_module.gpt_generate_room_title(messages) == "Rust lifetimes"
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["system", "user"]
    transcript = sent["messages"][1]["content"]
    assert transcript.index("ada:") < transcript.index("hermes:")


def test_a_failing_title_never_breaks_the_reply(test_app, monkeypatch):
    """Titling is best effort: a model reply with no content (only
    reasoning) raises inside the title helper, never out of a reply."""
    import app as app_module
    from models import Room, db

    room = make_room("no-title-room", message="hello")

    def broken(messages):
        raise AttributeError("'NoneType' object has no attribute 'replace'")

    monkeypatch.setattr(app_module, "gpt_generate_room_title", broken)
    app_module.title_untitled_room(room.name)
    db.session.expire_all()
    assert Room.query.filter_by(name="no-title-room").one().title is None


def test_private_chats_stay_out_of_browser_history(client):
    """A private room never enters localStorage history, so a guest who
    uses this browser after its owner signs out cannot read one."""
    me = make_user("me@example.test", "me")
    make_room("my-secret", owner=me, private=True)
    make_room("open-room")
    sign_in(client, me)
    assert "isPrivate: true" in client.get("/chat/my-secret").get_data(as_text=True)
    assert "isPrivate: false" in client.get("/chat/open-room").get_data(as_text=True)


SIDEWAYS_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    if (innerWidth <= 768) openDrawer('rooms');
    const sidebar = document.getElementById('app-sidebar');
    const edge = sidebar.getBoundingClientRect().right + 1;
    for (const el of [sidebar, ...sidebar.querySelectorAll('*')]) {
        const style = getComputedStyle(el);
        // .chat-list-name clips with "…" on purpose; nothing may scroll.
        if (el.scrollWidth > el.clientWidth + 1 && /auto|scroll/.test(style.overflowX)) {
            problems.push('scrolls sideways: ' + el.className + ' ' + el.scrollWidth + ' > ' + el.clientWidth);
        }
        if (el.getBoundingClientRect().right > edge && style.display !== 'none') {
            problems.push('past our sidebar: ' + el.tagName + '.' + el.className);
        }
    }
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.slice(0, 3).join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [375, 1280])
@pytest.mark.parametrize("path", ["/", "/chat/long-room", "/browse"])
def test_our_sidebar_never_scrolls_sideways(client, tmp_path, path, width):
    """A long unbroken title ends in "…" inside our sidebar; it once widened
    the grid's auto track & scrolled the whole sidebar sideways."""
    from models import Room, db

    me = make_user("a-very-long-display-name-without-spaces@example.test", "x" * 50)
    word = "supercalifragilisticexpialidocious" * 3
    room = make_room("long-room", owner=me)
    room.title = f"A title long enough to wrap {word}"
    room.active_users = ",".join(f"guest{n}" for n in range(12))
    make_room("another-room", owner=me, private=True)
    db.session.commit()
    sign_in(client, me)
    html = client.get(path).get_data(as_text=True)
    out = run_probe(html, SIDEWAYS_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


def test_join_titles_an_untitled_room_in_the_background(test_app, monkeypatch):
    """A room whose replies came before titling gets a title when someone
    opens it, so Your chats & Public chats stop showing its slug."""
    import app as app_module
    from models import Message, db

    room = make_room("old-untitled")
    for who, text in [
        ("ada", "How do lifetimes work?"),
        ("hermes", "They bound refs."),
    ]:
        db.session.add(Message(username=who, content=text, room_id=room.id))
    db.session.commit()
    spawned = []
    monkeypatch.setattr(
        app_module.gevent, "spawn", lambda fn, *args: spawned.append((fn, args))
    )
    socket = app_module.socketio.test_client(
        app_module.app, flask_test_client=test_app.test_client()
    )
    socket.emit("join", {"room_name": "old-untitled", "username": "ada"})
    assert (app_module.title_untitled_room, ("old-untitled",)) in spawned


def test_backfill_titles_every_untitled_conversation(test_app, monkeypatch):
    import app as app_module
    from models import Message, Room, db

    talked = make_room("talked")
    for text in ("hi", "hello"):
        db.session.add(Message(username="ada", content=text, room_id=talked.id))
    make_room("one-message", message="hi")
    titled = make_room("already")
    titled.title = "Kept"
    db.session.commit()
    monkeypatch.setattr(app_module, "gpt_generate_room_title", lambda m: "Greetings")
    monkeypatch.setattr(app_module.socketio, "emit", lambda *a, **k: None)
    assert app_module.backfill_titles() == (1, 1)
    db.session.expire_all()
    titles = {r.name: r.title for r in Room.query.all()}
    assert titles == {"talked": "Greetings", "one-message": None, "already": "Kept"}


def test_a_late_auto_title_never_replaces_one_set_meanwhile(test_app, monkeypatch):
    """The title model call yields: if /title new (or another titler) saves a
    title meanwhile, our late automatic title is dropped, never written over it."""
    import app as app_module
    from models import Room, db

    room = make_room("raced-room", message="hello")
    emitted = []

    def slow_model(messages):
        # Someone sets a title while our model call is still running.
        db.session.execute(
            db.update(Room).where(Room.id == room.id).values(title="Chosen by hand")
        )
        db.session.commit()
        return "Automatic"

    monkeypatch.setattr(app_module, "gpt_generate_room_title", slow_model)
    monkeypatch.setattr(
        app_module.socketio, "emit", lambda event, *a, **k: emitted.append(event)
    )
    app_module.title_untitled_room(room.name)
    db.session.expire_all()
    assert Room.query.filter_by(name="raced-room").one().title == "Chosen by hand"
    assert "update_room_title" not in emitted
