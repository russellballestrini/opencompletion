"""A message that was edited & resent carries a version pager: ‹ opens the
earlier version & what was said after it under the message, read only, &
› puts it away. An open version offers Continue from here: a new chat from
that version. Run: make test-ui"""

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


VERSIONS_PROBE = r"""
<script>
window.addEventListener('load', async () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const asked = [];
    window.fetch = (url) => {
        asked.push(url);
        return Promise.resolve({ok: true, json: () => Promise.resolve({versions: [
            [{id: 1, username: 'ada', content: 'list three muppets', from_model: false},
             {id: 2, username: 'qwen', content: 'Elmo, **Grover**, Oscar <img src=x onerror="window.pwned=1">', from_model: true}],
            [{id: 3, username: 'ada', content: 'list two muppets', from_model: false}],
        ]})});
    };
    const settle = () => new Promise((done) => setTimeout(done, 50));

    testSocket.deliver('previous_messages', {id: 7, username: 'bob', content: 'never edited', versions: 1});
    testSocket.deliver('chat_message', {id: 5, username: 'ada', content: 'list one muppet', versions: 3});
    check(!document.querySelector('#message-7 .message-versions'), 'a message never edited has no pager');
    const pager = document.querySelector('#message-5 .message-versions');
    check(Boolean(pager), 'an edited message has a pager');
    if (pager) {
        const [older, newer] = pager.querySelectorAll('button');
        const place = () => pager.querySelector('.message-versions-place').textContent;
        const panel = () => document.querySelector('#message-5 .earlier-version');
        check(place() === '3/3' && newer.disabled && !older.disabled, 'starts on our current version (' + place() + ')');
        check(asked.length === 0, 'nothing is fetched until asked');

        older.click(); await settle();
        check(asked.join() === '/api/messages/5/versions', 'asks for this message (' + asked.join() + ')');
        check(place() === '2/3', 'steps back one (' + place() + ')');
        check(panel() && panel().textContent.includes('list two muppets'), 'shows version 2');
        check(panel() && /Version 2 of 3/.test(panel().textContent) && /Read only/.test(panel().textContent), 'says which version & that it is read only');

        older.click(); await settle();
        check(place() === '1/3' && older.disabled, 'stops at the first (' + place() + ')');
        check(panel() && panel().textContent.includes('list three muppets') && panel().textContent.includes('Oscar'), 'shows version 1 with its reply');
        check(panel() && !panel().textContent.includes('list two muppets'), 'one version at a time');
        check(panel() && panel().querySelector('strong') && !window.pwned && !panel().querySelector('[onerror]'), 'renders markdown, sanitised');
        check(panel() && panel().querySelector('.is-model') && panel().querySelector('.is-person'), 'people & models read apart');
        check(panel() && !panel().querySelector('.button-container'), 'an earlier version has no actions');
        check(asked.length === 1, 'fetched once');
        check(document.documentElement.scrollWidth <= window.innerWidth, 'no sideways scroll');
        if (window.innerWidth <= 768) {
            const box = older.getBoundingClientRect();
            check(box.height >= 40 && box.width >= 40, 'a 40px tap target on phones (' + box.width + 'x' + box.height + ')');
        }

        newer.click(); await settle(); newer.click(); await settle();
        check(place() === '3/3' && !panel(), 'back on our current version, panel gone');
        check(document.querySelector('#message-5 > .message-content').textContent.includes('list one muppet'), 'our message itself never changed');
    }
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [320, 1280])
def test_an_edited_message_pages_through_its_earlier_versions(client, tmp_path, width):
    html = client.get("/chat/lobby").get_data(as_text=True)
    out = run_probe(html, VERSIONS_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


CONTINUE_PROBE = r"""
<script>
window.addEventListener('load', async () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const posted = [];
    const said = [];
    const opened = [];
    let refuse = true;
    window.alert = (text) => said.push(text);
    window.openChat = (name) => opened.push(name);
    window.fetch = (url, options) => {
        const reply = (ok, body) => Promise.resolve({ok, status: ok ? 200 : 429, json: () => Promise.resolve(body)});
        if (!options || options.method !== 'POST') {
            return reply(true, {versions: [
                [{id: 1, username: 'ada', content: 'list three muppets', from_model: false},
                 {id: 2, username: 'qwen', content: 'Elmo, Grover, Oscar', from_model: true}],
                [{id: 3, username: 'ada', content: 'list two muppets', from_model: false}],
            ]});
        }
        posted.push([url, options.body]);
        if (refuse) return reply(false, {error: 'Too many forks from your address'});
        return reply(true, {success: true, room: {name: 'lobby-fork-abc123'}});
    };
    // Replies resolve off no timer: yield by message until one lands.
    const turn = () => new Promise(resolve => {
        const channel = new MessageChannel();
        channel.port1.onmessage = resolve;
        channel.port2.postMessage(0);
    });
    const until = async (done, name) => {
        for (let i = 0; i < 2000; i++) {
            if (done()) return;
            await turn();
        }
        problems.push('never: ' + name);
    };

    testSocket.deliver('chat_message', {id: 5, username: 'ada', content: 'list one muppet', versions: 3});
    const pager = document.querySelector('#message-5 .message-versions');
    check(Boolean(pager), 'an edited message has a pager');
    if (pager) {
        const [older, newer] = pager.querySelectorAll('button');
        const panel = () => document.querySelector('#message-5 .earlier-version');
        const action = () => document.querySelector('#message-5 .earlier-version-actions button');
        check(!action(), 'our current version offers no Continue from here');

        older.click();
        await until(() => panel(), 'version 2 opens');
        const button = action();
        check(button && button.textContent === 'Continue from here', 'an open version offers Continue from here');
        if (button) {
            check(/new chat/.test(button.title) && /version 2/.test(button.title) && /public chatroom/.test(button.title) && /stays as it is/.test(button.title), 'its title says what happens (' + button.title + ')');
            check(!/private|\bAI\b/i.test(button.title + button.textContent), 'Solo, never Private');
            check(panel().lastElementChild === button.parentElement, 'it ends the version');
            check(document.documentElement.scrollWidth <= window.innerWidth, 'no sideways scroll');
            if (window.innerWidth <= 768) {
                check(button.getBoundingClientRect().height >= 40, 'a 40px tap target on phones (' + button.getBoundingClientRect().height + ')');
            }

            button.click();
            check(button.disabled, 'no second click while it is asked');
            await until(() => said.length === 1, 'a refusal is said');
            check(posted.length === 1 && posted[0][0] === '/api/messages/5/versions/2/fork', 'asks for version 2 of this message (' + posted.map(p => p[0]).join() + ')');
            check(posted.length === 1 && posted[0][1] === '{"private":false}', 'a guest asks for a public chatroom (' + (posted[0] || [])[1] + ')');
            check(/Too many forks/.test(said[0] || ''), 'says why it was refused (' + said[0] + ')');
            check(!button.disabled && opened.length === 0 && Boolean(panel()), 'refused: we stay, & may try again');
        }

        refuse = false;
        older.click();
        await until(() => panel() && /Version 1 of 3/.test(panel().textContent), 'version 1 opens');
        const first = action();
        check(first && /version 1/.test(first.title), 'each version continues from itself');
        if (first) {
            first.click();
            await until(() => opened.length === 1, 'our new chat opens');
            check(posted.length === 2 && posted[1][0] === '/api/messages/5/versions/1/fork', 'asks for version 1 (' + posted.map(p => p[0]).join() + ')');
            check(opened[0] === 'lobby-fork-abc123', 'opens the chat our server made (' + opened[0] + ')');
        }
        check(testSocket.sent.every(([event]) => event !== 'edit_and_resend' && event !== 'delete_message'), 'nothing here changes our chat');
        check(document.querySelector('#message-5 > .message-content').textContent.includes('list one muppet'), 'our message itself never changed');

        newer.click();
        newer.click();
        await until(() => !panel(), 'back on our current version');
        check(!action(), 'back on our current version, the offer is gone');
    }
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [320, 1280])
def test_an_open_earlier_version_continues_in_a_new_chat(client, tmp_path, width):
    html = client.get("/chat/lobby").get_data(as_text=True)
    out = run_probe(html, CONTINUE_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)
