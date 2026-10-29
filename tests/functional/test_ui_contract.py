"""Our page contract: every page shares one shell, one stylesheet & honest
copy, and lays out on a phone. docs/STYLEGUIDE.md is the prose version.

Server-side checks use Flask's test client on an in-memory database. Layout
checks load each rendered page in headless Chromium (tests/functional/
browser.py) at phone, tablet & desktop widths.

Run: make test-ui
"""

import os
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from browser import run_probe  # tests/functional/browser.py

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
STYLESHEET = ROOT / "static" / "css" / "style.css"
pytestmark = pytest.mark.functional

# Every page a person can open; chat is its own app shell.
PAGES = ["/", "/browse", "/search", "/search?keywords=zzz", "/profile", "/auth"]
ALL_PAGES = PAGES + ["/styleguide", "/chat/lobby"]
WIDTHS = [320, 375, 768, 1280]


@pytest.fixture
def client(test_app):
    return test_app.test_client()


def make_user(email, name):
    from models import User, db

    user = User(email=email, display_name=name)
    db.session.add(user)
    db.session.commit()
    return user


def make_room(name, owner=None, private=False, archived=False, message=None):
    from models import Message, Room, db

    room = Room(
        name=name,
        is_private=private,
        is_archived=archived,
        owner_id=owner.id if owner else None,
    )
    db.session.add(room)
    db.session.commit()
    if message:
        db.session.add(Message(username="someone", content=message, room_id=room.id))
        db.session.commit()
    return room


def sign_in(client, user):
    with client.session_transaction() as session:
        session["user_id"] = user.id
        session["user_email"] = user.email
        session["display_name"] = user.display_name


class _VisibleText(HTMLParser):
    """Text a person reads: body text & alt/title/placeholder/aria-label &
    <title>/<meta content>, never script or style source."""

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        attrs = dict(attrs)
        for key in ("alt", "title", "placeholder", "aria-label"):
            if attrs.get(key):
                self.parts.append(attrs[key])
        if tag == "meta" and attrs.get("content"):
            self.parts.append(attrs["content"])

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def visible_text(html):
    parser = _VisibleText()
    parser.feed(html)
    return " ".join(parser.parts)


# ---------------------------------------------------------------------------
# One shell, one stylesheet
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_PAGES)
def test_every_page_shares_our_shell(client, path):
    response = client.get(path)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '<meta name="viewport" content="width=device-width' in html
    assert html.count("/static/css/style.css") == 1
    assert "/static/js/site.js" in html
    # Our same brand link & site links on every page.
    assert '<a class="brand" href="/">OpenCompletion</a>' in html
    assert 'href="/browse"' in html
    assert "data-theme-toggle" in html


@pytest.mark.parametrize("template", sorted(p.name for p in TEMPLATES.glob("*.html")))
def test_templates_carry_no_styles_or_colours_of_their_own(template):
    """Look & feel lives in style.css: no <style> blocks, no raw colours."""
    source = (TEMPLATES / template).read_text()
    assert "<style" not in source
    for style in re.findall(r'style="([^"]*)"', source):
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb", style), (template, style)


def test_every_css_variable_used_is_defined():
    """A var(--x) with no definition silently renders transparent."""
    css = STYLESHEET.read_text()
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    used = set(re.findall(r"var\((--[\w-]+)", css))
    for template in TEMPLATES.glob("*.html"):
        source = template.read_text()
        used |= set(re.findall(r"var\((--[\w-]+)", source))
        defined |= set(re.findall(r"(--[\w-]+)\s*:", source))
    # styleguide.html builds --{{ token }} names from a list
    used = {name for name in used if "{" not in name}
    assert used <= defined, sorted(used - defined)


def test_only_chat_locks_page_scrolling():
    """html/body scroll on every page; only body.chat-page is an app shell."""
    css = STYLESHEET.read_text()
    base = re.search(r"html, body \{(.*?)\}", css, re.S).group(1)
    assert "overflow" not in base
    chat = re.search(r"body\.chat-page \{(.*?)\}", css, re.S).group(1)
    assert "overflow: hidden" in chat


# ---------------------------------------------------------------------------
# Honest copy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_PAGES)
def test_copy_says_machine_learning_not_ai(client, path):
    text = visible_text(client.get(path).get_data(as_text=True))
    assert not re.search(r"\bAI\b", text), re.findall(r".{30}\bAI\b.{30}", text)


def test_help_message_names_only_real_commands():
    import app as app_module

    documented = set(re.findall(r"`(/[a-z0-9]+)", app_module.HELP_MESSAGE))
    source = (ROOT / "app.py").read_text()
    handled = set(re.findall(r'command\.startswith\("(/[a-z0-9]+)', source))
    assert documented and documented <= handled, documented - handled
    assert not re.search(r"\bAI\b", app_module.HELP_MESSAGE)
    # Activities start from our room controls; /activity & /s3 still work
    # but go unlisted. /help names only what people type.
    assert documented == {"/title", "/cancel", "/help"}, documented


def test_sign_in_says_where_the_code_went(client):
    with patch("auth.send_otp_email", return_value="console"):
        data = client.post("/auth/send-otp", json={"email": "a@example.test"}).json
    assert data["delivery"] == "console"
    with patch("auth.send_otp_email", return_value="email"):
        data = client.post("/auth/send-otp", json={"email": "a@example.test"}).json
    assert data["delivery"] == "email"
    script = (ROOT / "static" / "js" / "auth.js").read_text()
    assert "data.delivery === 'console'" in script


@pytest.mark.parametrize(
    "value, expected",
    [
        ("/browse", "/browse"),
        ("/chat/a?b=c", "/chat/a?b=c"),
        ("", "/"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example", "/"),
        ("/\t/evil.example", "/"),  # browsers drop the tab: //evil.example
        ("/\n/evil.example", "/"),
        ("/%09/evil.example", "/%09/evil.example"),  # still encoded: a path
        ("/x\\y", "/"),
        ("javascript:alert(1)", "/"),
    ],
)
def test_sign_in_returns_only_to_our_own_pages(value, expected):
    import app as app_module

    assert app_module.safe_next_url(value) == expected


# ---------------------------------------------------------------------------
# Private rooms stay private
# ---------------------------------------------------------------------------


def test_search_never_reveals_someone_elses_private_room(client):
    owner = make_user("owner@example.test", "owner")
    make_room("open-room", message="zebra crossing")
    make_room("secret-room", owner=owner, private=True, message="zebra secret")
    make_room("old-room", archived=True, message="zebra archived")

    guest_html = client.get("/search?keywords=zebra").get_data(as_text=True)
    assert "secret-room" not in guest_html
    assert "old-room" not in guest_html

    sign_in(client, owner)
    owner_html = client.get("/search?keywords=zebra").get_data(as_text=True)
    assert "secret-room" in owner_html and "open-room" in owner_html
    assert "old-room" not in owner_html


def test_search_only_hit_redirects_into_that_room(client):
    make_room("only-room", message="quokka")
    response = client.get("/search?keywords=quokka")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/chat/only-room")


def test_private_room_updates_stay_inside_the_room(test_app):
    import app as app_module
    from models import Room

    socketio = MagicMock()
    with patch.dict(test_app.extensions, {"socketio": socketio}):
        app_module.emit_room_list_update(
            Room(name="mine", is_private=True), {"name": "mine"}
        )
        app_module.emit_room_list_update(
            Room(name="ours", is_private=False), {"name": "ours"}
        )
    calls = socketio.emit.call_args_list
    assert calls[0].kwargs["room"] == "mine"
    assert calls[1].kwargs["room"] is None


# ---------------------------------------------------------------------------
# Inline scripts parse
# ---------------------------------------------------------------------------


def require_node():
    node = shutil.which("node")
    if not node:
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("node is required to syntax-check page scripts")
        pytest.skip("node is required to syntax-check page scripts")
    return node


@pytest.mark.parametrize(
    "script",
    sorted(str(p.relative_to(ROOT)) for p in (ROOT / "static" / "js").rglob("*.js")),
)
def test_static_scripts_parse_and_hold_no_template_syntax(script):
    source = ROOT / script
    text = source.read_text()
    # Jinja never renders static files; server values go via CHAT_CONFIG.
    assert "{{" not in text and "{%" not in text
    result = subprocess.run(
        [require_node(), "--check", str(source)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path", ALL_PAGES)
def test_inline_scripts_parse(client, path, tmp_path):
    node = require_node()
    html = client.get(path).get_data(as_text=True)
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts
    for index, script in enumerate(scripts):
        source = tmp_path / f"script{index}.js"
        source.write_text(script)
        result = subprocess.run(
            [node, "--check", str(source)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Layout on real screens
# ---------------------------------------------------------------------------

LAYOUT_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const width = innerWidth;
    if (document.documentElement.scrollWidth > width) {
        const wide = [...document.querySelectorAll('body *')]
            .filter(el => el.getBoundingClientRect().right > width + 1)
            .slice(0, 3).map(el => el.tagName + '.' + el.className + '#' + el.id);
        problems.push('horizontal overflow ' + document.documentElement.scrollWidth + ' > ' + width + ' ' + wide.join(' '));
    }
    const visible = el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
    };
    if (width <= 768) {
        for (const el of document.querySelectorAll('.btn, .icon-btn, .sidebar-link, .chat-list a, .sidebar-user-link, .form-input')) {
            if (visible(el) && el.getBoundingClientRect().height < 40) {
                problems.push('small tap target ' + el.tagName + ' "' + el.textContent.trim().slice(0, 20) + '" ' + el.getBoundingClientRect().height);
            }
        }
    }
    if (!document.body.classList.contains('chat-page') && getComputedStyle(document.body).overflowY === 'hidden') {
        problems.push('page cannot scroll');
    }
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("path", PAGES + ["/styleguide"])
def test_pages_fit_every_screen(client, tmp_path, path, width):
    make_room("a-public-room-with-a-rather-long-name-to-wrap", message="zzz")
    make_room("another-room", message="zzz")
    html = client.get(path).get_data(as_text=True)
    out = run_probe(html, LAYOUT_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


CHAT_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const shown = el => getComputedStyle(el).display !== 'none' && el.getBoundingClientRect().width > 0;
    const inside = el => { const r = el.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth + 1; };
    const rooms = document.getElementById('app-sidebar');
    const controls = document.querySelector('.utility-belt');
    const toggles = [...document.querySelectorAll('.drawer-toggle')];
    check(document.documentElement.scrollWidth <= innerWidth, 'horizontal overflow');
    check(shown(document.getElementById('message')), 'message box visible');
    check(shown(document.getElementById('model-select')), 'model picker in our composer');
    check(document.querySelector('#public-chats a[aria-current="page"]'), 'open chat marked in our sidebar');
    if (innerWidth <= 768) {
        check(toggles.length === 2 && toggles.every(shown), 'drawer toggles visible');
        const main = document.querySelector('.chat-main').getBoundingClientRect();
        check(main.left === 0 && main.width >= innerWidth - 1, 'chat fills a phone: ' + main.width);
        check(!shown(rooms) && !shown(controls), 'panels start closed');
        openDrawer('rooms');
        check(shown(rooms) && inside(rooms), 'rooms drawer opens on screen');
        check(shown(rooms.querySelector('#public-chats')) && shown(rooms.querySelector('.sidebar-actions')), 'chats & site links in drawer');
        check(shown(rooms.querySelector('.sidebar-user')), 'you, at the bottom of the drawer');
        closeDrawer();
        openDrawer('controls');
        check(shown(controls) && inside(controls), 'controls drawer opens on screen');
        check(!!controls.querySelector('.download-links'), 'history download reachable');
        document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));
        check(!shown(controls), 'Escape closes drawer');
    } else {
        check(toggles.every(t => !shown(t)), 'no drawer toggles on wide screens');
        check(shown(rooms) && shown(controls), 'both sidebars visible');
    }
    document.getElementById('auto-play-tts-btn').click();
    const readAloud = document.getElementById('auto-play-tts-btn');
    check(readAloud.dataset.on === 'true' && readAloud.textContent === 'Stop reading replies aloud', 'toggle reports its state');
    showAuthModal();
    check(shown(document.getElementById('auth-step-email')), 'sign-in dialog opens');
    check(inside(document.querySelector('#auth-modal .modal-card')), 'sign-in dialog fits');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", WIDTHS)
def test_chat_works_on_every_screen(client, tmp_path, width):
    make_room("lobby", message="hello")
    html = client.get("/chat/lobby").get_data(as_text=True)
    out = run_probe(html, CHAT_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


MESSAGE_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const chat = document.getElementById('chat');
    const deliver = (event, data) => testSocket.deliver(event, data);

    // Rendering: markdown, code blocks & their buttons
    deliver('chat_message', {id: 1, username: 'ada', content: '**bold** words\n\n```python\nprint(1)\n```'});
    const first = document.getElementById('message-1');
    check(first && first.querySelector('strong'), 'markdown renders');
    check(first.querySelector('pre code.hljs'), 'code highlighted');
    check(first.querySelector('.copy-button') && first.querySelector('.play-button'), 'code block buttons');
    check(!first.querySelector('a[href^="/profile/"]'), 'no link to a profile page that does not exist');

    // Sanitizing: live, historical & edited messages, image shortcut included
    const attacks = [
        '<img src="data:image/jpeg;base64,AAAA" onerror="window.pwned=1">',
        '<img src=x onerror="window.pwned=2">',
        '<a href="javascript:window.pwned=3">click</a>',
        '<script>window.pwned=4<\/script>',
        '<iframe src="https://evil.example" style="position:fixed;inset:0"></iframe>',
        '<div style="position:fixed;inset:0;background:red">overlay</div>',
    ];
    attacks.forEach((content, i) => deliver('chat_message', {id: 10 + i, username: 'mallory', content}));
    attacks.forEach((content, i) => deliver('previous_messages', {id: 20 + i, username: 'mallory', content}));
    deliver('message_updated', {message_id: 1, content: attacks[0], username: 'ada'});
    check(!chat.querySelector('[onerror], script, a[href^="javascript:"], iframe, [style*="fixed"]'), 'hostile markup removed');
    check(chat.querySelector('#message-10 img[src^="data:image/jpeg"]'), 'data: images still show');
    check(chat.querySelector('#message-20 img[src^="data:image/jpeg"]'), 'historical data: images still show');
    const handlers = [...chat.querySelectorAll('*')].flatMap(el => [...el.attributes].map(a => a.name)).filter(n => n.startsWith('on'));
    check(handlers.length === 0, 'no on* handler attributes: ' + handlers.join(','));

    // Copy gives feedback on its own button (window.event is gone by then)
    navigator.clipboard.writeText = () => Promise.resolve();
    const copyButton = document.querySelector('#message-1 .button-container .copy-button');
    copyButton.click();

    // foxhop is our default voice, whatever order the voice list comes in
    check(defaultVoice(['kokoro:aira', 'kokoro:foxhop', 'tts-1:alloy']) === 'kokoro:foxhop', 'foxhop by default');
    check(defaultVoice(['kokoro:aira', 'kokoro:bella']) === 'kokoro:aira', 'first voice when foxhop is missing');

    // A retitled room renames itself in our sidebar & top bar
    const row = document.querySelector('#public-chats li[data-room-id]');
    // An owned public room is listed twice (Your chats & Public chats).
    const twin = row.cloneNode(true);
    document.getElementById('your-chats').appendChild(twin);
    deliver('update_room_list', {id: Number(row.dataset.roomId), name: 'lobby', title: 'Weekend <b>plans</b>'});
    check(twin.textContent.includes('Weekend'), 'every row of a room is renamed');
    deliver('update_room_title', {title: 'Weekend <b>plans</b>'});
    check(row.textContent.includes('Weekend <b>plans</b>') && !row.querySelector('b'), 'sidebar title is text, never markup');
    check(document.getElementById('chat-title').textContent === 'Weekend <b>plans</b>', 'top bar shows the title');

    // A name full of markdown stays a name
    deliver('chat_message', {id: 30, username: '[x](javascript:alert(1))', content: 'hi'});
    check(!chat.querySelector('#message-30 a'), 'markdown in a name makes no link');

    // Streaming: chunks accumulate into one rendered message
    deliver('message_chunk', {id: 40, username: 'hermes', model_name: 'hermes', content: 'Hello '});
    deliver('message_chunk', {id: 40, username: 'hermes', model_name: 'hermes', content: '*world*', is_complete: true});
    const streamed = document.querySelector('#message-40 .message-content');
    check(streamed && streamed.querySelector('em') && streamed.textContent.includes('Hello world'), 'chunks stream in');

    // Sending: what we type goes to our room, as us
    const box = document.getElementById('message');
    box.value = 'from the probe';
    document.getElementById('send-button').click();
    const sent = testSocket.sent.find(([event]) => event === 'chat_message');
    check(sent && sent[1].message === 'from the probe' && sent[1].room_name === 'lobby', 'send emits chat_message');
    check(box.value === '', 'box clears after send');

    // Fork lives on model replies, never on people's messages or our controls
    deliver('previous_messages', {id: 60, username: 'hermes', content: 'Big Bird', from_model: true});
    deliver('previous_messages', {id: 61, username: 'ada', content: 'more', from_model: false});
    const fork = document.querySelector('#message-60 .fork-button');
    check(fork && /new chat from this reply/.test(fork.title), 'a model reply offers Fork, explained on hover');
    check(!document.querySelector('#message-61 .fork-button'), 'a person\'s message has no Fork');
    check(document.querySelector('#message-40 .fork-button'), 'a streamed reply offers Fork');
    check(![...document.querySelectorAll('.utility-belt button')].some(b => /fork/i.test(b.textContent)), 'no Fork in room controls');
    const forkCalls = [];
    let forkAlert = null;
    window.alert = message => { forkAlert = message; };
    const fetchBeforeFork = window.fetch;
    window.fetch = (url, options) => {
        if (String(url).includes('/fork')) {
            forkCalls.push([String(url), JSON.parse(options.body)]);
            return Promise.resolve({ok: false, status: 429, json: () => Promise.resolve({error: 'Too many forks from your address'})});
        }
        return fetchBeforeFork(url, options);
    };
    fork.click();

    // Stop shows while a reply streams & stops every reply in this room
    const stop = document.getElementById('stop-button');
    check(stop.hidden, 'no Stop when nothing streams');
    deliver('replies_streaming', {count: 1});
    check(!stop.hidden && getComputedStyle(stop).display !== 'none', 'Stop shows while a reply streams');
    stop.click();
    check(testSocket.sent.some(([event, data]) => event === 'stop_replies' && data.room_name === 'lobby'), 'Stop asks our server to end it');
    deliver('replies_streaming', {count: 0});
    check(stop.hidden, 'Stop hides when replies end');

    // Files: dropped on our box, refused over our limit, sent as links
    const form = document.getElementById('message-form');
    const drop = files => {
        const transfer = new DataTransfer();
        files.forEach(f => transfer.items.add(f));
        form.dispatchEvent(new DragEvent('drop', {dataTransfer: transfer, bubbles: true, cancelable: true}));
    };
    const limit = Number(form.dataset.maxUpload);
    check(limit === 7 * 1024 * 1024, 'our 7 MB limit reaches the page: ' + limit);
    drop([new File(['x'.repeat(10)], 'notes (v2).txt', {type: 'text/plain'})]);
    check(form.querySelectorAll('.composer-file').length === 1, 'a dropped file waits as a chip');
    const huge = new File(['x'], 'huge.bin');
    Object.defineProperty(huge, 'size', {value: limit + 1});
    drop([huge]);
    check(form.querySelectorAll('.composer-file').length === 1
        && /huge\.bin/.test(document.querySelector('.composer-status').textContent), 'an oversized file is refused, by name');
    const uploads = [];
    const fetchBeforeUpload = window.fetch;
    window.fetch = (url, options) => {
        if (String(url).endsWith('/api/rooms/lobby/attachments')) {
            uploads.push(options.body.get('file').name);
            return Promise.resolve({ok: true, json: () => Promise.resolve(
                {id: 5, url: '/attachments/5/notes (v2).txt', filename: 'notes (v2).txt', size: 10, is_image: false})});
        }
        return fetchBeforeUpload(url, options);
    };
    box.value = 'see attached';
    document.getElementById('send-button').click();
    let fileSent = null;

    // Auto-fix runs only our marked post's exact block, never a same-named
    // guest's other code (guest names are not unique)
    const executed = [];
    const realFetch = window.fetch;
    window.fetch = (url, options) => {
        if (String(url).includes('/api/code/execute')) {
            executed.push(JSON.parse(options.body).code.trim());
            return Promise.reject(new Error('offline'));
        }
        return realFetch(url, options);
    };
    window.pendingAutoExec = {marker: '<!-- autofix:probe-7 -->', code: 'print(2)', attempt: 1};
    const twoBlocks = '```python\nprint(1)\n```\n\n```python\nprint(2)\n```';
    deliver('chat_message', {id: 50, username, content: twoBlocks});
    check(window.pendingAutoExec, 'unmarked look-alike leaves the fix pending');
    deliver('chat_message', {id: 51, username, content: '**Auto-fixed code:** <!-- autofix:probe-7 -->\n\n' + twoBlocks});
    check(!window.pendingAutoExec, 'our marked post consumes the fix');

    // Async effects (clipboard promise, the auto-run's fetch) settle at their
    // own pace: poll until both have or 1.5 s pass, never a fixed sleep.
    const started = Date.now();
    const settle = () => {
        fileSent = fileSent || testSocket.sent.find(([event, data]) => event === 'chat_message' && data.message.startsWith('see attached'));
        if ((executed.length === 0 || copyButton.textContent !== 'Copied!' || !forkAlert || !fileSent) && Date.now() - started < 1500) {
            setTimeout(settle, 50);
            return;
        }
        check(executed.length === 1 && executed[0] === 'print(2)', 'only the fixed block ran: ' + JSON.stringify(executed));
        check(copyButton.textContent === 'Copied!', 'copy shows Copied!: ' + copyButton.textContent);
        check(!window.pwned, 'no injected script ran (' + window.pwned + ')');
        check(uploads.length === 1 && uploads[0] === 'notes (v2).txt', 'our file uploaded on send: ' + JSON.stringify(uploads));
        check(fileSent && fileSent[1].message.includes('(/attachments/5/notes%20%28v2%29.txt)'), 'the message links it, escaped: ' + (fileSent && fileSent[1].message));
        check(!form.querySelector('.composer-file'), 'sent files leave the box');
        check(forkCalls.length === 1 && forkCalls[0][0] === '/api/messages/60/fork' && forkCalls[0][1].private === false, 'Fork posts this reply, public for a guest: ' + JSON.stringify(forkCalls));
        check(/Too many forks/.test(forkAlert || '') && !fork.disabled, 'a refused fork says why & can be retried');
        document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
    };
    setTimeout(settle, 50);
});
</script>
"""


def test_chat_renders_sanitizes_streams_and_sends(client, tmp_path):
    make_room("lobby")
    html = client.get("/chat/lobby").get_data(as_text=True)
    out = run_probe(html, MESSAGE_PROBE, tmp_path, 1280)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


MENU_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const sidebar = document.getElementById('app-sidebar');
    const toggle = document.querySelector('.page-topbar .drawer-toggle');
    const shown = el => el.getBoundingClientRect().height > 0;
    const links = ['/', '/search', '/browse', '/profile', '/auth?next=/browse'];
    links.forEach(href => check(sidebar.querySelector(`a[href="${href}"]`), 'sidebar links ' + href));
    check(sidebar.querySelector('[data-theme-toggle]'), 'theme toggle in our sidebar');
    check(sidebar.querySelector('a[href="/browse"][aria-current="page"]'), 'current page marked');
    if (innerWidth <= 768) {
        check(shown(toggle) && !shown(sidebar), 'sidebar folds behind ☰');
        toggle.click();
        check(shown(sidebar) && sidebar.getBoundingClientRect().right <= innerWidth, 'drawer opens on screen');
        document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));
        check(!shown(sidebar), 'Escape closes it');
    } else {
        check(!shown(toggle) && shown(sidebar), 'desktop shows our sidebar, no menu button');
    }
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", WIDTHS)
def test_sidebar_menu_on_every_page(client, tmp_path, width):
    html = client.get("/browse").get_data(as_text=True)
    out = run_probe(html, MENU_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


def test_models_endpoint_answers(client):
    """/models refreshes our model map on a timer kept in app.py."""
    response = client.get("/models")
    assert response.status_code == 200
    assert "models" in response.json


CHAT_ACTIONS_PROBE = r"""
<script>
window.addEventListener('load', async () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    window.onunhandledrejection = (e) => { document.body.dataset.probe = 'failed: ' + e.reason; };
    // Our server, as the menu sees it: a rename answers with its title.
    const calls = [];
    window.fetch = async (url, options) => {
        calls.push([url, options && options.method, options && options.body]);
        return new Response(JSON.stringify({title: 'Garden plans'}), {status: 200, headers: {'Content-Type': 'application/json'}});
    };
    window.prompt = () => 'Garden plans';
    try { localStorage.removeItem('pinnedChats'); } catch (e) {}

    const card = document.querySelector('.room-card-wrap[data-room-name="lobby"]');
    check(card && card.querySelector('[data-chat-actions]'), 'room cards carry ⋯');
    const row = document.querySelector('#public-chats li[data-room-name="lobby"]');
    check(row && row.querySelector('[data-chat-actions]'), 'sidebar rows carry ⋯');

    const open = button => { button.click(); return document.querySelector('.chat-actions-menu'); };
    const labels = menu => [...menu.querySelectorAll('button')].map(b => b.textContent);
    let menu = open(card.querySelector('[data-chat-actions]'));
    check(menu && labels(menu).join() === 'Rename,Refresh title,Pin', 'an unowned room offers rename, refresh & pin: ' + (menu && labels(menu)));
    const box = menu.getBoundingClientRect();
    check(box.left >= 0 && box.right <= innerWidth && box.bottom <= innerHeight, 'menu stays on screen');

    // Pin: our sidebar's Pinned section shows the chat
    [...menu.querySelectorAll('button')].find(b => b.textContent === 'Pin').click();
    await new Promise(r => setTimeout(r, 0));
    const pinned = document.getElementById('pinned-chats-section');
    check(!pinned.hidden && pinned.querySelector('[data-room-name="lobby"]'), 'pinned chat shows in Pinned');
    menu = open(card.querySelector('[data-chat-actions]'));
    check(labels(menu).includes('Unpin'), 'a pinned chat offers Unpin');
    document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));
    check(!document.querySelector('.chat-actions-menu'), 'Escape closes the menu');

    // Rename: every place showing the chat takes its new title
    menu = open(row.querySelector('[data-chat-actions]'));
    [...menu.querySelectorAll('button')].find(b => b.textContent === 'Rename').click();
    await new Promise(r => setTimeout(r, 20));
    check(calls.some(([url, method]) => url.endsWith('/title') && method === 'POST'), 'rename posts its title');
    check(row.querySelector('.chat-list-name').textContent === 'Garden plans', 'sidebar row renamed');
    check(card.querySelector('.room-card-title').textContent === 'Garden plans', 'room card renamed');

    // Your own room also offers archive & delete
    const mine = document.querySelector('.room-card-wrap[data-room-name="adas-room"]');
    menu = open(mine.querySelector('[data-chat-actions]'));
    check(labels(menu).includes('Archive') && labels(menu).includes('Delete'), 'owner may archive & delete');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [320, 1280])
def test_chat_actions_menu(client, tmp_path, width):
    ada = make_user("ada@example.test", "ada")
    make_room("lobby", message="hi")
    make_room("adas-room", owner=ada)
    with client.session_transaction() as session:
        session["user_id"] = ada.id
    html = client.get("/browse").get_data(as_text=True)
    out = run_probe(html, CHAT_ACTIONS_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


RESEND_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const deliver = (event, data) => testSocket.deliver(event, data);

    // Edit & resend on a person's message, never on a model's reply
    deliver('chat_message', {id: 1, username: 'ada', content: 'list three muppets'});
    deliver('message_chunk', {id: 2, username: 'qwen', model_name: 'qwen', content: 'Elmo', is_complete: true});
    document.querySelector('#message-1 .edit-button').click();
    check(document.querySelector('#message-1 .resend-button'), 'a person\'s message offers Save & resend');
    document.querySelector('#message-2 .edit-button').click();
    check(!document.querySelector('#message-2 .resend-button'), 'a model reply is only edited in place');
    document.querySelector('#message-1 textarea').value = 'list two muppets';
    document.querySelector('#message-1 .resend-button').click();
    check(document.querySelector('#message-1 textarea'), 'the editor stays open until the server answers');
    check(document.querySelector('meta[name="theme-color"]').content !== '', 'theme-color follows our theme');
    const sent = testSocket.sent.find(([event]) => event === 'edit_and_resend');
    check(sent && sent[1].message_id === 1 && sent[1].content === 'list two muppets' && sent[1].room_name === 'lobby', 'resend emits edit_and_resend');
    deliver('messages_hidden', {ids: [1, 2], revision_id: 3});
    check(!document.getElementById('message-1') && !document.getElementById('message-2'), 'hidden messages leave the page');

    // Follow-ups: chips under our newest reply; a click sends that question
    deliver('chat_message', {id: 3, username: 'ada', content: 'list two muppets'});
    deliver('message_chunk', {id: 4, username: 'qwen', model_name: 'qwen', content: 'Elmo, Oscar', is_complete: true});
    deliver('followup_suggestions', {message_id: 4, suggestions: ['Who is <b>Oscar</b>?', 'Why green?']});
    const chips = document.querySelectorAll('#message-4 .followup-chip');
    check(chips.length === 2 && !document.querySelector('#message-4 .followups b'), 'chips show as text');
    deliver('chat_message', {id: null, content: 'bob joined the room'});
    check(document.querySelectorAll('#message-4 .followup-chip').length === 2, 'a notice with no id leaves the chips');
    deliver('followup_suggestions', {message_id: 4, suggestions: ['Who is Oscar?', 'Why green?']});
    check(document.querySelectorAll('.followup-chip').length === 2, 'late suggestions still land past a notice');
    chips[1].click();
    const asked = testSocket.sent.filter(([event]) => event === 'chat_message').pop();
    check(asked && asked[1].message === 'Why green?', 'a chip sends its question');
    check(!document.querySelector('.followups'), 'chips go once one is used');
    deliver('followup_suggestions', {message_id: 3, suggestions: ['stale']});
    check(!document.querySelector('.followups'), 'suggestions for an older message are ignored');

    // Thinking: kept from history (collapsed), & left open on a stopped reply
    deliver('previous_messages', {id: 70, username: 'qwen', content: 'Elmo!', from_model: true, thinking: 'Elmo is <b>red</b>.'});
    const kept = document.querySelector('#message-70 .message-thinking');
    check(kept && !kept.open && kept.textContent.includes('Elmo is <b>red</b>.') && !kept.querySelector('b'), 'history keeps thinking, collapsed, as text');
    deliver('message_chunk', {id: 71, reasoning_content: 'Telly is a purple? '});
    deliver('message_chunk', {id: 71, username: 'qwen', model_name: 'qwen', content: '_Stopped: qwen kept repeating_', is_first_chunk: true, keep_thinking_open: true});
    check(document.querySelector('#message-71 .message-thinking').open, 'a stopped reply keeps its thinking open');
    deliver('message_chunk', {id: 72, reasoning_content: 'Big Bird is yellow. '});
    deliver('message_chunk', {id: 72, username: 'qwen', model_name: 'qwen', content: 'Big Bird!', is_first_chunk: true});
    check(!document.querySelector('#message-72 .message-thinking').open, 'an answer collapses its thinking');

    // ↓ shows only once we scroll up from the newest message
    const chat = document.getElementById('chat');
    const down = document.getElementById('scroll-bottom-button');
    for (let i = 10; i < 60; i++) deliver('chat_message', {id: i, username: 'bob', content: 'line ' + i});
    chat.scrollTop = chat.scrollHeight;
    chat.dispatchEvent(new Event('scroll'));
    check(down.hidden, 'no ↓ at the bottom');
    chat.scrollTop = 0;
    chat.dispatchEvent(new Event('scroll'));
    check(!down.hidden, '↓ once scrolled up');
    const box = down.getBoundingClientRect();
    check(box.width > 0 && box.bottom <= document.getElementById('message-form').getBoundingClientRect().top + 1, '↓ floats above the composer');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [320, 1280])
def test_edit_resend_followups_and_scroll_down(client, tmp_path, width):
    make_room("lobby")
    html = client.get("/chat/lobby").get_data(as_text=True)
    out = run_probe(html, RESEND_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


SIGN_IN_CENTRED_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const card = document.getElementById('auth-flow').getBoundingClientRect();
    const middle = (card.top + card.bottom) / 2;
    if (innerWidth > 768) {
        check(Math.abs(middle - innerHeight / 2) < innerHeight * 0.15,
              `card centred top to bottom (middle ${middle}, screen ${innerHeight})`);
        check(document.documentElement.scrollHeight <= innerHeight + 1, 'no page scroll');
    } else {
        check(card.top < innerHeight / 3, 'phones keep the card at the top');
    }
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [375, 1280])
def test_sign_in_card_is_centred_beside_our_sidebar(client, tmp_path, width):
    html = client.get("/auth").get_data(as_text=True)
    out = run_probe(html, SIGN_IN_CENTRED_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


def test_static_links_carry_a_version_that_follows_the_file(client):
    """A deploy's new scripts reach browsers at once: every /static/ link
    names its file's content hash."""
    html = client.get("/chat/lobby").get_data(as_text=True)
    versions = re.findall(r'/static/js/chat/(\d-[a-z]+\.js)\?v=([0-9a-f]{10})"', html)
    assert len(versions) >= 8
    assert len({v for _, v in versions}) == len(versions)  # each its own hash
