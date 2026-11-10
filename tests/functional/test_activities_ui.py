"""Our Activities gallery in a real browser: its kind & text filters, and
Play, which makes a public chatroom & hands chat the `/activity <path>`
command the way home hands over a first message.

Run: make test-ui
"""

import re

import pytest

from browser import run_probe  # tests/functional/browser.py

pytestmark = pytest.mark.functional


@pytest.fixture
def client(test_app, monkeypatch):
    # research/*.yaml: every activity we ship.
    monkeypatch.setitem(test_app.config, "LOCAL_ACTIVITIES", True)
    return test_app.test_client()


def test_our_sidebar_links_the_gallery_and_marks_it_current(client):
    html = client.get("/activities").get_data(as_text=True)
    assert re.search(r'href="/activities"\s+aria-current="page"', html)
    assert 'href="/activities"' in client.get("/browse").get_data(as_text=True)


def test_every_shipped_activity_has_a_card_but_our_spec(client):
    html = client.get("/activities").get_data(as_text=True)
    paths = re.findall(r'data-path="([^"]+)"', html)
    assert len(paths) > 50 and len(paths) == len(set(paths))
    assert "research/activity52-vault-heist.yaml" in paths
    assert "research/SPEC.yaml" not in paths
    # Each of them offers Play & says its kind.
    assert html.count("data-play") == len(paths)
    assert set(re.findall(r'<article[^>]* data-kind="([^"]*)"', html)) == {
        "game",
        "lesson",
        "tool",
    }


FILTER_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const cards = [...document.querySelectorAll('.activity-card')];
    const shown = () => cards.filter(card => !card.hidden);
    const option = kind => document.querySelector(`.segmented-option[data-kind="${kind}"]`);
    const text = document.getElementById('activities-filter');
    const empty = document.getElementById('activities-empty');
    const type = words => { text.value = words; text.dispatchEvent(new Event('input', {bubbles: true})); };
    const count = kind => Number(option(kind).querySelector('.segmented-count').textContent);

    check(cards.length > 50 && shown().length === cards.length, 'opens on every activity');
    check(count('all') === cards.length, 'All counts every card');
    check(empty.hidden, 'no empty state while cards show');

    // By kind: only that kind, as many as its count says.
    for (const kind of ['game', 'lesson', 'tool']) {
        option(kind).click();
        check(option(kind).getAttribute('aria-checked') === 'true' && option('all').getAttribute('aria-checked') === 'false', kind + ' is the one checked');
        check(shown().length === count(kind) && shown().length > 0, kind + ' count matches: ' + shown().length);
        check(shown().every(card => card.dataset.kind === kind), 'only ' + kind + 's show');
    }
    check(count('game') + count('lesson') + count('tool') === cards.length, 'every card has a kind');

    // By words, in a title...
    option('all').click();
    type('  BATTLESHIP ');
    check(shown().length >= 1 && shown().every(card => /battleship/i.test(card.dataset.title + card.dataset.description + card.dataset.name)), 'title filter: ' + shown().length);
    check(shown().some(card => card.dataset.path === 'research/activity29-battleship.yaml'), 'Battleship found by its title');
    // ...in a description alone...
    type('shared tries');
    check(shown().length === 1 && shown()[0].dataset.title === 'Vault Heist', 'description filter: ' + shown().map(c => c.dataset.title));
    // ...& both filters together.
    option('lesson').click();
    check(shown().length === 0 && !empty.hidden, 'a game is no lesson: empty state');
    check(/No lessons match “shared tries”/.test(empty.textContent), 'empty state says what: ' + empty.textContent.trim());
    document.getElementById('activities-clear').click();
    check(shown().length === cards.length && empty.hidden && text.value === '', 'Show every activity clears both');
    check(option('all').getAttribute('aria-checked') === 'true', 'back on All');

    type('zzz no such activity');
    check(shown().length === 0 && /No activities match/.test(empty.textContent), 'no match says so');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [320, 1280])
def test_cards_filter_by_kind_and_by_words(client, tmp_path, width):
    html = client.get("/activities").get_data(as_text=True)
    out = run_probe(html, FILTER_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


PLAY_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const created = [], titled = [];
    window.fetch = (url, options) => {
        if (String(url) === '/api/rooms/create') {
            const body = JSON.parse(options.body);
            created.push(body);
            return Promise.resolve({ok: true, json: () => Promise.resolve({room: {id: 41, name: body.name, is_private: false}})});
        }
        if (String(url) === '/api/rooms/41/title') {
            titled.push(JSON.parse(options.body));
            return Promise.resolve({ok: true, json: () => Promise.resolve({title: 'Vault Heist'})});
        }
        return Promise.reject(new TypeError('offline'));
    };
    let opened = null;
    window.startChatNavigate = url => { opened = url; };

    const card = document.querySelector('.activity-card[data-path="research/activity52-vault-heist.yaml"]');
    const play = card.querySelector('[data-play]');
    check(play.textContent.trim() === 'Play' && /Play Vault Heist in a new chatroom/.test(play.getAttribute('aria-label')), 'Play says what it opens');
    check(play.getBoundingClientRect().height >= 44, 'Play is a 44px tap target: ' + play.getBoundingClientRect().height);
    check(card.querySelector('.badge').textContent.trim() === 'Game', 'its kind badge');
    play.click();
    play.click();  // a double tap makes one room
    const started = Date.now();
    const settle = () => {
        if (!opened && Date.now() - started < 1500) { setTimeout(settle, 20); return; }
        check(created.length === 1, 'one room made: ' + created.length);
        const room = created[0] || {};
        check(/^vault-heist-[a-z0-9]+$/.test(room.name), 'named after the activity: ' + room.name);
        check(room.is_private === false, 'a public chatroom');
        check(titled.length === 1 && titled[0].title === 'Vault Heist', 'titled after the activity');
        const pending = JSON.parse(sessionStorage.getItem('pendingMessage') || 'null');
        check(pending && pending.room === room.name, 'handed to that room');
        check(pending && pending.message === '/activity research/activity52-vault-heist.yaml', 'the command chat sends: ' + (pending && pending.message));
        check(pending && pending.model === 'None', 'no model is asked to answer a command');
        check(opened === '/chat/' + room.name, 'opens the new room: ' + opened);
        // Chat takes it exactly once, for this room only (utils.js).
        check(takePendingMessage('another-room') === null, 'not for another room');
        const taken = takePendingMessage(room.name);
        check(taken && taken.message === pending.message && !sessionStorage.getItem('pendingMessage'), 'taken once');
        document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
    };
    setTimeout(settle, 20);
});
</script>
"""


@pytest.mark.parametrize("width", [320, 1280])
def test_play_makes_a_chatroom_and_hands_chat_the_activity(client, tmp_path, width):
    html = client.get("/activities").get_data(as_text=True)
    out = run_probe(html, PLAY_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


PLAY_FAILS_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    window.fetch = () => Promise.resolve({ok: false, json: () => Promise.resolve({error: 'Room names use lower case letters'})});
    let opened = null;
    window.startChatNavigate = url => { opened = url; };
    const play = document.querySelector('.activity-card [data-play]');
    play.click();
    setTimeout(() => {
        const error = document.getElementById('activities-error');
        check(error.textContent === 'Room names use lower case letters', 'says what failed: ' + error.textContent);
        check(error.getBoundingClientRect().height > 0, 'where a person sees it');
        check(!opened && !sessionStorage.getItem('pendingMessage'), 'nothing opened or handed over');
        check(!play.disabled, 'Play can be tried again');
        document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
    }, 200);
});
</script>
"""


def test_a_room_that_cannot_be_made_says_so_and_opens_nothing(client, tmp_path):
    html = client.get("/activities").get_data(as_text=True)
    out = run_probe(html, PLAY_FAILS_PROBE, tmp_path, 375)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


def test_the_command_play_hands_over_is_the_one_chat_starts_an_activity_with(
    test_app, monkeypatch
):
    """Play's pending message reaches `dispatch_message` as any chat message
    does; there `/activity <path>` starts that activity in its room."""
    import app as app_module
    from models import Room, db

    room = Room(name="vault-heist-abc123")
    db.session.add(room)
    db.session.commit()
    started = []
    monkeypatch.setattr(
        app_module.gevent, "spawn", lambda fn, *a: started.append((fn, a))
    )
    app_module.dispatch_message(
        room, "ada", "/activity research/activity52-vault-heist.yaml", ["None"]
    )
    assert started == [
        (
            app_module.activity.start_activity,
            ("vault-heist-abc123", "research/activity52-vault-heist.yaml", "ada"),
        )
    ]
