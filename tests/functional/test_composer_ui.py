"""Our composer on a phone, in headless Chromium: its voice & model
pickers can be told apart & read (one row clipped them to "tts-…" &
"Peopl…"), & a draft that grows never covers our newest message.
Run: make test-ui"""

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


def verdict(out):
    """A probe's whole verdict, so a failure names every check it failed."""
    found = re.search(r'data-probe="([^"]*)"', out)
    return found.group(1) if found else "no verdict: the probe never finished"


# VOICE_ID & MODEL_ID stand for the two selects' ids (home & chat differ).
PICKERS_PROBE = r"""
<script>
// Our speech service's voices, cached as utils.js caches them, so the
// voice picker shows without a network.
localStorage.setItem('voicesList', JSON.stringify({data: [{id: 'tts-1-f5', voices: ['aria', 'foxhop']}]}));
localStorage.setItem('voicesListExpiration', String(Date.now() + 60000));
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const phone = innerWidth <= 480;
    const voice = document.getElementById('VOICE_ID');
    const model = document.getElementById('MODEL_ID');
    const send = document.querySelector('.composer-send');
    const bar = document.querySelector('.composer-bar').getBoundingClientRect();
    const box = el => el.getBoundingClientRect();
    const size = el => Math.round(box(el).width) + 'x' + Math.round(box(el).height);

    check(document.documentElement.scrollWidth <= innerWidth, 'no sideways scroll (' + document.documentElement.scrollWidth + ')');
    [[voice, 'Voice'], [model, 'Model']].forEach(([select, name]) => {
        const pick = select.closest('.composer-pick');
        check(pick, name + ' sits in a .composer-pick');
        if (!pick) return;
        // What tells them apart when words are clipped: an icon each, &
        // a name a screen reader says.
        const icon = pick.querySelector('svg');
        check(icon && box(icon).width >= 14 && box(icon).height >= 14, name + ' has an icon');
        const said = pick.querySelector('.visually-hidden');
        check(select.labels.length === 1 && said && said.textContent.trim() === name, name + ' is labelled');
        check(!select.hidden && box(pick).left >= bar.left - 1 && box(pick).right <= bar.right + 1, name + ' stays inside our bar: ' + size(pick));
        if (phone) check(box(pick).height >= 44, name + ' is a 44px tap target: ' + size(pick));
    });
    const icons = [voice, model].map(select => select.closest('.composer-pick').querySelector('svg').innerHTML);
    check(icons[0] !== icons[1], 'their icons differ');
    check(box(voice).right <= box(model).left + 1, 'voice sits left of the model');

    check(box(send).right <= innerWidth && box(send).bottom <= innerHeight && box(send).width >= 40, 'Send is in view: ' + size(send));
    document.querySelectorAll('.composer-tool:not([hidden])').forEach(tool => {
        check(box(tool).height >= 40 && box(tool).left >= bar.left - 1, 'a tool keeps its size: ' + size(tool));
    });
    if (phone) {
        // Two rows: the pickers, then tools & Send; the model gets the room.
        check(box(model).bottom <= box(send).top + 1, 'our pickers have a row of their own above Send');
        check(box(model).width >= 120, 'our model picker is wide enough to read: ' + size(model));
        check(box(voice).width >= 60, 'our voice picker is wide enough to read: ' + size(voice));
        check(box(model).width >= box(voice).width, 'the model gets more room than the voice');
        const picks = box(model.closest('.composer-pick')).right - box(voice.closest('.composer-pick')).left;
        check(picks >= bar.width * 0.9, 'together they span our bar (' + Math.round(picks) + ' of ' + Math.round(bar.width) + 'px)');
    } else {
        // Desktop keeps one row.
        const middle = el => (box(el).top + box(el).bottom) / 2;
        check(Math.abs(middle(model) - middle(send)) <= 2 && Math.abs(middle(voice) - middle(send)) <= 2, 'one row on a wide screen');
        check(bar.height <= 48, 'our bar is one row tall (' + Math.round(bar.height) + ')');
    }

    // No voices: nothing of the voice picker is left, its icon included.
    voice.hidden = true;
    check(getComputedStyle(voice.closest('.composer-pick')).display === 'none', 'no voices, no voice icon');
    check(box(model).width >= (phone ? 200 : 60), 'the model takes the row then: ' + size(model));
    check(document.documentElement.scrollWidth <= innerWidth, 'still no sideways scroll');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [320, 375, 1280])
@pytest.mark.parametrize(
    "path,voice,model",
    [
        ("/", "composer-voice", "composer-model"),
        ("/chat/lobby", "voice-select", "model-select"),
    ],
)
def test_pickers_can_be_told_apart_and_read(
    client, tmp_path, width, path, voice, model
):
    html = client.get(path).get_data(as_text=True)
    probe = PICKERS_PROBE.replace("VOICE_ID", voice).replace("MODEL_ID", model)
    out = run_probe(html, probe, tmp_path, width)
    assert verdict(out) == "passed"


GROW_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const chat = document.getElementById('chat');
    const box = document.getElementById('message');
    const fromBottom = () => Math.round(chat.scrollHeight - chat.scrollTop - chat.clientHeight);
    const type = text => { box.value = text; box.dispatchEvent(new Event('input')); };
    const draft = 'one\ntwo\nthree\nfour\nfive\nsix';
    // ResizeObserver answers after layout: give each step a few frames.
    const settle = () => new Promise(done => setTimeout(done, 150));
    (async () => {
        for (let i = 0; i < 40; i++) {
            testSocket.deliver('chat_message', {id: 100 + i, username: 'bob', content: 'filler ' + i});
        }
        testSocket.deliver('chat_message', {id: 1, username: 'ada', content: 'our newest message'});
        chat.scrollTop = chat.scrollHeight;
        await settle();
        check(chat.scrollHeight > chat.clientHeight + 200 && fromBottom() <= 1, 'starts at our newest message (' + fromBottom() + ')');

        const roomy = chat.clientHeight;
        type(draft);
        await settle();
        check(chat.clientHeight <= roomy - 60, 'a six-line draft takes height from the messages (' + roomy + ' to ' + chat.clientHeight + ')');
        check(fromBottom() <= 1, 'at the bottom, it stays there (' + fromBottom() + 'px off)');
        const newest = document.getElementById('message-1').getBoundingClientRect();
        const composer = document.getElementById('message-form').getBoundingClientRect();
        check(newest.bottom <= composer.top + 1, 'our newest message is not under the composer');

        // Nearly at the bottom (within 40px) counts as at the bottom.
        type('');
        await settle();
        chat.scrollTop = chat.scrollHeight - chat.clientHeight - 30;
        await settle();
        type(draft);
        await settle();
        check(fromBottom() <= 1, 'within 40px of the bottom, it is pinned too (' + fromBottom() + 'px off)');

        // Scrolled up to read: growing & shrinking leave that place alone.
        type('');
        await settle();
        chat.scrollTop = 120;
        await settle();
        type(draft);
        await settle();
        check(chat.scrollTop === 120, 'scrolled up, a growing draft moves nothing (' + chat.scrollTop + ')');
        type('');
        await settle();
        check(chat.scrollTop === 120, 'nor does a shrinking one (' + chat.scrollTop + ')');
        document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
    })();
});
</script>
"""


@pytest.mark.parametrize("width", [375, 1280])
def test_a_growing_draft_keeps_our_newest_message_in_view(client, tmp_path, width):
    html = client.get("/chat/lobby").get_data(as_text=True)
    out = run_probe(html, GROW_PROBE, tmp_path, width)
    assert verdict(out) == "passed"
