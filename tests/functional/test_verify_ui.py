"""Verify a transcript (/verify) in headless Chromium: a real JSON download
picked into our page gets its verdict & a line per message, a changed one
says which message failed & why, junk says what to do, & nothing is
fetched. The hashing itself is proved against chain.py in
tests/integration/test_verify_transcript.py. Run: make test-ui"""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from browser import run_probe  # noqa: E402  tests/functional/browser.py

pytestmark = pytest.mark.functional

WORDS = "café 🦊 with-a-rather-long-unbroken-word-" + "x" * 80
REPLY = "Voilà ✅"


@pytest.fixture
def transcript(test_app, monkeypatch):
    """A room's real JSON download: a person's message & a model's reply,
    chained as our app chains them."""
    import chain
    from models import Message, Room, db
    from routes import DEPS

    monkeypatch.setitem(DEPS, "system_users", ["qwen"])
    room = Room(name="lobby", title="Foxes & cafés")
    db.session.add(room)
    db.session.commit()
    for who, kind, words in (("ada", "user", WORDS), ("qwen", "assistant", REPLY)):
        message = Message(username=who, content=words, room_id=room.id)
        db.session.add(message)
        db.session.commit()
        chain.append(
            room.id,
            room.name,
            kind,
            messageId=message.id,
            username=who,
            contentSha256=chain.sha256_text(words),
        )
    response = test_app.test_client().get(
        "/download_chat_history", query_string={"room_name": "lobby"}
    )
    return response.get_data(as_text=True)


VERIFY_PROBE = r"""
<script>
window.addEventListener('load', async () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const done = () => { document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed'; };
    try {
        let asked = 0;
        window.fetch = () => { asked++; return Promise.reject(new TypeError('offline test page')); };
        XMLHttpRequest.prototype.open = () => { asked++; };
        navigator.sendBeacon = () => { asked++; return false; };

        const verdict = document.getElementById('verify-verdict');
        const input = document.getElementById('verify-file');
        const items = () => [...document.querySelectorAll('#verify-results .verify-item')];
        const pick = async (text, name) => {
            const before = verdict.textContent;
            const files = new DataTransfer();
            files.items.add(new File([text], name, {type: 'application/json'}));
            input.files = files.files;
            input.dispatchEvent(new Event('change', {bubbles: true}));
            for (let i = 0; i < 200; i++) {
                await new Promise(resolve => setTimeout(resolve, 5));
                if (verdict.textContent !== before && !verdict.textContent.startsWith('Checking ')) return;
            }
            problems.push('no verdict for ' + name);
        };
        const fits = (name) => check(document.documentElement.scrollWidth <= innerWidth, name + ': horizontal overflow ' + document.documentElement.scrollWidth);

        const button = document.getElementById('verify-pick');
        check(button.getBoundingClientRect().height >= 44, 'Choose is 44px tall');
        check(getComputedStyle(verdict).display === 'none', 'no verdict before a file');
        check(document.querySelector('.sidebar-link[href="/verify"][aria-current="page"]'), 'marked in our sidebar');
        fits('empty');

        await pick(REAL, 'lobby.json');
        check(verdict.dataset.verdict === 'verified', 'real: ' + verdict.dataset.verdict + ' ' + verdict.textContent);
        check(verdict.textContent.startsWith('Every message verified: 2 messages'), 'real verdict words: ' + verdict.textContent);
        check(verdict.classList.contains('alert-success'), 'real verdict is a success alert');
        check(items().length === 2 && items().every(li => li.dataset.state === 'verified'), 'two verified lines');
        check(items()[0].textContent.includes('ada') && items()[0].textContent.includes('Verified'), 'a line says who & Verified');
        const facts = document.getElementById('verify-facts');
        check(!facts.hidden && facts.textContent.includes(ROOT_HASH) && facts.textContent.includes('Foxes & cafés'), 'root & room shown');
        check(facts.textContent.includes('/chain.jsonl'), 'names where our copy of its chain is');
        fits('verified');

        await pick(TAMPERED, 'changed.json');
        check(verdict.dataset.verdict === 'failed' && verdict.classList.contains('alert-error'), 'tampered: ' + verdict.dataset.verdict);
        check(verdict.textContent.startsWith('1 message of 2 failed'), 'tampered verdict words: ' + verdict.textContent);
        check(items().length === 2 && items()[0].dataset.state === 'verified' && items()[1].dataset.state === 'failed', 'only the changed message fails');
        check(items()[1].textContent.includes('Failed: the words do not match the digest the chain recorded.'), 'says which check failed: ' + items()[1].textContent);
        fits('failed');

        await pick('# Chat History for lobby\n\n### User: ada (Turn 1)\n', 'lobby.md');
        check(verdict.classList.contains('alert-error') && verdict.textContent.includes('Markdown transcript'), 'markdown named: ' + verdict.textContent);
        check(items().length === 0 && facts.hidden, 'junk clears the last result');
        fits('junk');

        await pick('{"$schema": "other/1"}', 'other.json');
        check(verdict.textContent.includes('"other/1"'), 'wrong format named: ' + verdict.textContent);
        check(asked === 0, 'nothing leaves the page (' + asked + ' requests)');
    } catch (error) {
        problems.push('probe threw: ' + error.message);
    }
    done();
});
</script>
"""


@pytest.mark.parametrize("width", [320, 1280])
def test_a_dropped_transcript_gets_its_verdict(test_app, transcript, tmp_path, width):
    doc = json.loads(transcript)
    assert doc["chain"] and all(m["chain"] for m in doc["messages"])
    tampered = json.loads(transcript)
    tampered["messages"][1]["content"] = "Voila ✅"
    html = test_app.test_client().get("/verify").get_data(as_text=True)

    def js(value):
        return json.dumps(value).replace("</", "<\\/")

    probe = VERIFY_PROBE.replace(
        "<script>",
        "<script>\nconst REAL = %s;\nconst TAMPERED = %s;\nconst ROOT_HASH = %s;"
        % (
            js(transcript),
            js(json.dumps(tampered, ensure_ascii=False)),
            js(doc["chain"]["root"]),
        ),
        1,
    )
    out = run_probe(html, probe, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


def test_the_page_says_what_it_does_not_prove(test_app):
    html = test_app.test_client().get("/verify").get_data(as_text=True)
    assert "never uploaded" in html
    assert "What is not checked" in html
    assert "/api/rooms/&lt;id&gt;/chain.jsonl" in html and "outside witness" in html
    assert "/static/js/verify.js" in html
    # Our page asks our server for nothing: no form, no fetch of its own
    assert "<form" not in html[html.index('id="main"') :]
    script = (Path(__file__).resolve().parents[2] / "static/js/verify.js").read_text()
    assert not re.search(r"\bfetch\(|XMLHttpRequest|sendBeacon|WebSocket", script)


def test_chat_and_our_sidebar_lead_to_it(test_app):
    from models import Room, db

    db.session.add(Room(name="lobby2"))
    db.session.commit()
    chat = test_app.test_client().get("/chat/lobby2").get_data(as_text=True)
    links = chat[chat.index('class="download-links"') :][:700]
    assert 'href="/verify"' in links
    assert 'class="sidebar-link" href="/verify"' in chat
