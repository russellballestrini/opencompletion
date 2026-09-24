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
from unittest.mock import patch

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

    with patch.object(app_module, "socketio") as socketio:
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
        for (const el of document.querySelectorAll('.btn, .icon-btn, .site-nav a, .site-nav button, .form-input')) {
            if (visible(el) && el.getBoundingClientRect().height < 40) {
                problems.push('small tap target ' + el.tagName + ' "' + el.textContent.trim().slice(0, 20) + '" ' + el.getBoundingClientRect().height);
            }
        }
    }
    const header = document.querySelector('.site-header');
    if (header && getComputedStyle(document.body).overflowY === 'hidden') {
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
    const rooms = document.getElementById('rooms-list');
    const controls = document.querySelector('.utility-belt');
    const toggles = [...document.querySelectorAll('.drawer-toggle')];
    check(document.documentElement.scrollWidth <= innerWidth, 'horizontal overflow');
    check(shown(document.getElementById('message')), 'message box visible');
    if (innerWidth <= 768) {
        check(toggles.length === 2 && toggles.every(shown), 'drawer toggles visible');
        check(!shown(rooms) && !shown(controls), 'panels start closed');
        openDrawer('rooms');
        check(shown(rooms) && inside(rooms), 'rooms drawer opens on screen');
        check(shown(rooms.querySelector('#room-tabs')), 'public/private tabs in drawer');
        check(shown(rooms.querySelector('.site-nav')), 'site links in drawer');
        closeDrawer();
        openDrawer('controls');
        check(shown(controls) && inside(controls), 'controls drawer opens on screen');
        check(shown(document.getElementById('model-select')), 'model picker reachable');
        check(!!controls.querySelector('.download-links'), 'history download reachable');
        document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));
        check(!shown(controls), 'Escape closes drawer');
    } else {
        check(toggles.every(t => !shown(t)), 'no drawer toggles on wide screens');
        check(shown(rooms) && shown(controls), 'both sidebars visible');
    }
    document.getElementById('auto-play-tts-btn').click();
    check(document.getElementById('auto-play-tts-btn').getAttribute('aria-pressed') === 'true', 'toggle reports its state');
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
    ];
    attacks.forEach((content, i) => deliver('chat_message', {id: 10 + i, username: 'mallory', content}));
    attacks.forEach((content, i) => deliver('previous_messages', {id: 20 + i, username: 'mallory', content}));
    deliver('message_updated', {message_id: 1, content: attacks[0], username: 'ada'});
    check(!chat.querySelector('[onerror], script, a[href^="javascript:"]'), 'hostile markup removed');
    check(chat.querySelector('#message-10 img[src^="data:image/jpeg"]'), 'data: images still show');
    check(chat.querySelector('#message-20 img[src^="data:image/jpeg"]'), 'historical data: images still show');
    const handlers = [...chat.querySelectorAll('*')].flatMap(el => [...el.attributes].map(a => a.name)).filter(n => n.startsWith('on'));
    check(handlers.length === 0, 'no on* handler attributes: ' + handlers.join(','));

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

    setTimeout(() => {
        check(!window.pwned, 'no injected script ran (' + window.pwned + ')');
        document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
    }, 300);
});
</script>
"""


def test_chat_renders_sanitizes_streams_and_sends(client, tmp_path):
    make_room("lobby")
    html = client.get("/chat/lobby").get_data(as_text=True)
    out = run_probe(html, MESSAGE_PROBE, tmp_path, 1280)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)
