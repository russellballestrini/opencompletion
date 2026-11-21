"""Deleting (or archiving) a chat in headless Chromium: it shows at once,
before our server answers. Every row & card for that chat dims, strikes
through & spins; when the server agrees they fold away (or, for the open
chat, home opens); when it does not, they come back as they were.
Run: make test-ui"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from browser import run_probe  # noqa: E402  tests/functional/browser.py
from test_ui_contract import make_room, make_user, sign_in  # noqa: E402

pytestmark = pytest.mark.functional


@pytest.fixture
def client(test_app):
    return test_app.test_client()


# Our server answers each delete only when the probe says so; `said()`
# is what a failure tells the person (a toast). `agree()` answers our
# in-page confirm, whose close event lands a task later.
SERVER_STUB = r"""
    const answers = [];
    const said = () => [...document.querySelectorAll('.toast.is-error[role="alert"] .toast-text')].map(t => t.textContent).join();
    window.fetch = (url, options) => {
        if (/\/api\/rooms\/\d+\/(delete|archive)$/.test(String(url))) {
            return new Promise(resolve => answers.push(ok => resolve(new Response(
                JSON.stringify(ok ? {ok: true} : {error: 'Our server said no'}),
                {status: ok ? 200 : 500, headers: {'Content-Type': 'application/json'}}))));
        }
        return Promise.reject(new TypeError('offline'));
    };
    const wait = (ms) => new Promise(r => setTimeout(r, ms || 20));
    const status = () => (document.getElementById('room-action-status') || {}).textContent;
    const agree = async (label) => {
        const yes = document.querySelector('dialog.ask-dialog[open] [data-ask="confirm"]');
        if (!yes || yes.textContent !== label || !yes.classList.contains('btn-danger')) throw new Error('no ' + label + ' confirm in our page');
        yes.click();
        await wait(0);
    };
"""

LIST_PROBE = (
    r"""
<script>
window.addEventListener('load', async () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    window.onunhandledrejection = (e) => { document.body.dataset.probe = 'failed: ' + e.reason; };
"""
    + SERVER_STUB
    + r"""
    const rows = () => [...document.querySelectorAll('[data-room-name="adas-room"]')];
    const card = document.querySelector('.room-card-wrap[data-room-name="adas-room"]');
    const row = document.querySelector('#your-chats li[data-room-name="adas-room"]');
    check(card && row, 'adas-room has a card & a sidebar row');
    const remove = async (from) => {
        from.querySelector('[data-chat-actions]').click();
        [...document.querySelectorAll('.chat-actions-menu button')].find(b => b.textContent === 'Delete').click();
        await agree('Delete');
    };

    // Asked first, in our page; Cancel deletes nothing.
    card.querySelector('[data-chat-actions]').click();
    [...document.querySelectorAll('.chat-actions-menu button')].find(b => b.textContent === 'Delete').click();
    check(!rows().some(r => r.classList.contains('room-removing')), 'nothing happens before an answer');
    document.querySelector('dialog.ask-dialog[open] [data-ask="cancel"]').click();
    await wait();
    check(!document.querySelector('dialog.ask-dialog') && answers.length === 0 && !rows().some(r => r.classList.contains('room-removing')), 'Cancel deletes nothing');

    // The click shows at once: every place showing the chat is busy.
    await remove(card);
    check(rows().every(r => r.classList.contains('room-removing') && r.inert && r.getAttribute('aria-busy') === 'true'), 'card & row marked deleting at once');
    check(status() === 'Deleting “adas-room”…', 'status says it: ' + status());
    check(getComputedStyle(card, '::after').animationName === 'ttsSpin', 'the card spins');
    check(getComputedStyle(row.querySelector('.chat-list-name')).textDecorationLine === 'line-through', 'its name is struck through');
    check(getComputedStyle(row).pointerEvents === 'none', 'nothing in it takes a click');
    await wait();
    check(answers.length === 1, 'one delete asked: ' + answers.length);

    // A failure brings it all back & says why.
    answers[0](false);
    await wait();
    check(rows().length === 2 && rows().every(r => !r.classList.contains('room-removing') && !r.inert && !r.hasAttribute('aria-busy')), 'rows restored');
    check(said() === 'Our server said no', 'error shown: ' + said());
    check(status() === '', 'status cleared');

    // Again from the sidebar, & it works: the rows fold away.
    await remove(row);
    await wait();
    answers[1](true);
    await wait();
    check(rows().every(r => r.classList.contains('room-leaving')), 'rows fold away');
    check(status() === 'Deleted “adas-room”.', 'status says done: ' + status());
    await wait(400);
    check(rows().length === 0, 'rows gone: ' + rows().length);
    check(document.querySelector('[data-room-name="lobby"]'), 'other chats stay');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""
)


def browse_as_ada(client):
    ada = make_user("ada@example.test", "ada")
    make_room("lobby", message="hi")
    make_room("adas-room", owner=ada)
    sign_in(client, ada)
    return client.get("/browse").get_data(as_text=True)


@pytest.mark.parametrize("width", [320, 1280])
def test_deleting_a_chat_shows_then_folds_or_comes_back(client, tmp_path, width):
    out = run_probe(browse_as_ada(client), LIST_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


REDUCED_PROBE = (
    r"""
<script>
window.addEventListener('load', async () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
"""
    + SERVER_STUB
    + r"""
    check(matchMedia('(prefers-reduced-motion: reduce)').matches, 'reduced motion forced');
    const card = document.querySelector('.room-card-wrap[data-room-name="adas-room"]');
    card.querySelector('[data-chat-actions]').click();
    [...document.querySelectorAll('.chat-actions-menu button')].find(b => b.textContent === 'Delete').click();
    await agree('Delete');
    check(card.classList.contains('room-removing'), 'still marked busy');
    check(getComputedStyle(card, '::after').animationName === 'none', 'no spin');
    await wait();
    answers[0](true);
    await wait();
    check(!document.querySelector('[data-room-name="adas-room"]'), 'gone at once, no fold');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""
)


def test_reduced_motion_removes_without_moving(client, tmp_path):
    out = run_probe(
        browse_as_ada(client),
        REDUCED_PROBE,
        tmp_path,
        1280,
        extra_flags=["--force-prefers-reduced-motion"],
    )
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


ROOM_PROBE = (
    r"""
<script>
window.addEventListener('load', async () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    window.onunhandledrejection = (e) => { document.body.dataset.probe = 'failed: ' + e.reason; };
"""
    + SERVER_STUB
    + r"""
    let left = null;
    window.roomLeftNavigate = (url) => { left = url; };
    const del = [...document.querySelectorAll('.room-actions-section button')].find(b => b.textContent === 'Delete');
    const archive = [...document.querySelectorAll('.room-actions-section button')].find(b => b.textContent === 'Archive');
    const row = document.querySelector('#your-chats li[data-room-name="adas-room"]');
    check(del && archive && row, 'room controls & sidebar row');

    // Inside the chat: the button spins & says so, the page dims.
    del.click();
    check(!del.disabled && !document.body.classList.contains('room-leaving-page'), 'nothing happens before an answer');
    await agree('Delete');
    check(del.getAttribute('aria-busy') === 'true' && del.disabled && del.textContent === 'Deleting…', 'Delete is busy: ' + del.textContent);
    check(archive.disabled, 'Archive waits too');
    check(document.body.classList.contains('room-leaving-page'), 'our page dims');
    check(row.classList.contains('room-removing'), 'its sidebar row is marked');
    check(getComputedStyle(del, '::before').animationName === 'ttsSpin', 'Delete spins');
    check(status() === 'Deleting “adas-room”…', 'status says it: ' + status());
    await wait();

    answers[0](false);
    await wait();
    check(!del.disabled && !archive.disabled && !del.hasAttribute('aria-busy') && del.textContent === 'Delete', 'buttons restored');
    check(!document.body.classList.contains('room-leaving-page') && !row.classList.contains('room-removing'), 'page & row restored');
    check(said() === 'Our server said no', 'error shown: ' + said());
    check(left === null, 'still here');

    del.click();
    await agree('Delete');
    await wait();
    answers[1](true);
    await wait();
    check(left === '/', 'home opens: ' + left);
    check(del.getAttribute('aria-busy') === 'true' && document.body.classList.contains('room-leaving-page'), 'busy until home takes over');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""
)


@pytest.mark.parametrize("width", [320, 1280])
def test_deleting_the_open_chat_shows_then_goes_home(client, tmp_path, width):
    ada = make_user("ada@example.test", "ada")
    make_room("adas-room", owner=ada)
    sign_in(client, ada)
    html = client.get("/chat/adas-room").get_data(as_text=True)
    out = run_probe(html, ROOM_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)
