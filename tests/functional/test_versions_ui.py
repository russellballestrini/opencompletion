"""A message that was edited & resent carries a version pager: ‹ opens the
earlier version & what was said after it under the message, read only, &
› puts it away. Run: make test-ui"""

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
