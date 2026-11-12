"""Stop on our chat page in headless Chromium: it shows at once, before
our server answers. In the click's frame Stop spins & reads "Stopping…"
(busy, disabled: a second press sends nothing) & each streaming reply dims
with a "stopping…" note; it clears when every reply here has ended, or when
our server says nothing was left; with no answer in time Stop comes back,
with a quiet note, to be pressed again. Run: make test-ui"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from browser import run_probe  # noqa: E402  tests/functional/browser.py

pytestmark = pytest.mark.functional


@pytest.fixture
def page(test_app):
    from models import Room, db

    db.session.add(Room(name="lobby"))
    db.session.commit()
    return test_app.test_client().get("/chat/lobby").get_data(as_text=True)


def probe(body):
    """A probe: two replies (9 & 10, 10 running a tool) are streaming when
    `body` runs; it has check(), deliver(), sent() & wait(), & may await."""
    return (
        r"""
<script>
window.addEventListener('load', async () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const deliver = (event, data) => testSocket.deliver(event, data);
    const sent = (name) => testSocket.sent.filter(([event]) => event === name);
    const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms || 20));
    const stop = document.getElementById('stop-button');
    const label = stop.querySelector('.composer-stop-label');
    const status = document.getElementById('stop-status');
    const reply = (id) => document.getElementById('message-' + id);
    const note = (id) => getComputedStyle(reply(id), '::after').content;
    const dim = (id) => getComputedStyle(reply(id).querySelector(':scope > .message-body')).opacity;
    const busy = () => stop.getAttribute('aria-busy') === 'true';
    try {
        deliver('replies_streaming', {count: 2});
        deliver('message_chunk', {id: 9, content: 'Once upon', username: 'ada', model_name: 'qwen', is_first_chunk: true});
        deliver('message_chunk', {id: 10, content: 'Let me look.', username: 'ada', model_name: 'qwen', is_first_chunk: true});
        deliver('tool_activity', {id: 10, tool: 'sandbox_shell', label: 'ls', where: 'sandbox', asker: 'ada', model_name: 'qwen', status: 'running', at: 12});
        check(!stop.hidden && !busy() && note(9) === 'none', 'two replies stream, Stop ready');
"""
        + body
        + r"""
    } catch (error) {
        problems.push('threw: ' + (error.stack || error));
    }
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""
    )


def passed(out):
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


CONFIRMED_PROBE = probe(r"""
    // The click's frame: no await between the click & these checks.
    stop.click();
    check(busy() && stop.disabled && stop.getAttribute('aria-label') === 'Stopping…', 'Stop is busy at once');
    check(getComputedStyle(label).display !== 'none' && label.textContent === 'Stopping…', 'it reads Stopping…');
    check(getComputedStyle(stop, '::before').animationName === 'ttsSpin', 'Stop spins');
    check(getComputedStyle(stop.querySelector('svg')).display === 'none', 'the square gives way');
    check(note(9) === '"stopping…"' && note(10) === '"stopping…"', 'each reply says it is stopping: ' + note(9));
    check(Number(dim(9)) < 1 && Number(dim(10)) < 1, 'each reply dims: ' + dim(9));
    check(getComputedStyle(reply(10).querySelector('.message-tool.is-running'), '::before').animationName === 'none', 'its running tool stills');
    check(status.textContent === 'Stopping 2 replies…' && status.getAttribute('aria-live') === 'polite', 'status says it: ' + status.textContent);
    check(!status.classList.contains('is-note'), 'said to screen readers, not shown');
    const box = document.getElementById('message-form').getBoundingClientRect();
    const own = stop.getBoundingClientRect();
    check(own.right <= box.right + 1 && own.left >= box.left - 1, 'Stopping… fits our box');
    check(document.documentElement.scrollWidth <= innerWidth, 'no sideways scroll: ' + document.documentElement.scrollWidth);

    // Pressed again (& again): one stop asked.
    stop.click();
    stop.click();
    stopReplies();
    check(sent('stop_replies').length === 1, 'one stop sent: ' + sent('stop_replies').length);

    // Our server got it, two still to end: still stopping.
    deliver('stop_received', {count: 2});
    check(busy(), 'still busy after the ack');

    // Reply 9 finishes on its own: it comes back, 10 still stopping.
    deliver('message_chunk', {id: 9, content: '', is_complete: true, final: 'Once upon a time.', model_name: 'qwen'});
    deliver('replies_streaming', {count: 1});
    check(note(9) === 'none' && dim(9) === '1', 'a finished reply is itself again');
    check(note(10) === '"stopping…"' && busy(), 'one left: still stopping');

    // The last one ends: Stop hides as it always has, nothing busy.
    deliver('replies_streaming', {count: 0});
    check(stop.hidden && !busy() && !stop.disabled, 'Stop clears & hides');
    check(stop.getAttribute('aria-label') === 'Stop the reply', 'its label is back');
    check(!document.getElementById('chat').classList.contains('replies-stopping'), 'no reply marked');
    check(status.textContent === 'Stopped.', 'status says so: ' + status.textContent);

    // The next reply's Stop works again.
    deliver('replies_streaming', {count: 1});
    stop.click();
    check(busy() && sent('stop_replies').length === 2, 'a new stop goes');
""")


@pytest.mark.parametrize("width", [320, 1280])
def test_stop_shows_at_once_and_clears_when_replies_end(page, tmp_path, width):
    passed(run_probe(page, CONFIRMED_PROBE, tmp_path, width))


NOTHING_LEFT_PROBE = probe(r"""
    stop.click();
    // The replies ended before the stop landed & that word never came.
    deliver('stop_received', {count: 0});
    check(!busy() && stop.hidden && status.textContent === 'Stopped.', 'nothing left to stop: cleared');
""")


@pytest.mark.parametrize("width", [320, 1280])
def test_stop_our_server_finds_nothing_to_stop_clears(page, tmp_path, width):
    passed(run_probe(page, NOTHING_LEFT_PROBE, tmp_path, width))


TIMEOUT_PROBE = probe(r"""
    stopConfirmMs = 150;
    // No answer at all (a dropped socket): Stop comes back with a note.
    testSocket.drop();
    stop.click();
    check(busy(), 'busy while it waits');
    await wait(250);
    check(!busy() && !stop.disabled && !stop.hidden, 'Stop is back to be pressed');
    check(stop.getAttribute('aria-label') === 'Stop the reply', 'its label is back');
    check(!document.getElementById('chat').classList.contains('replies-stopping'), 'replies no longer marked');
    check(/may not have reached our server/.test(status.textContent), 'says the stop may be lost: ' + status.textContent);
    check(status.classList.contains('is-note') && status.offsetHeight > 0, 'the note shows');
    check(document.documentElement.scrollWidth <= innerWidth, 'no sideways scroll');

    // Pressed again: a second stop goes, the note gives way.
    testSocket.land();
    stop.click();
    check(sent('stop_replies').length === 2 && busy(), 'pressed again, sent again');
    check(!status.classList.contains('is-note'), 'the note gives way');

    // Our server has it this time, but a reply hangs on: back again, saying so.
    deliver('stop_received', {count: 2});
    await wait(250);
    check(!busy() && /has the stop/.test(status.textContent), 'acked but not ended: ' + status.textContent);

    // Then the replies end: the note is moot.
    deliver('replies_streaming', {count: 0});
    check(stop.hidden && status.textContent === '' && !status.classList.contains('is-note'), 'note cleared when replies end');
""")


@pytest.mark.parametrize("width", [320, 1280])
def test_stop_unanswered_comes_back_to_be_pressed_again(page, tmp_path, width):
    passed(run_probe(page, TIMEOUT_PROBE, tmp_path, width))


REDUCED_PROBE = probe(r"""
    check(matchMedia('(prefers-reduced-motion: reduce)').matches, 'reduced motion forced');
    stop.click();
    check(busy() && stop.disabled && getComputedStyle(label).display !== 'none', 'still busy & reads Stopping…');
    check(getComputedStyle(stop, '::before').animationName === 'none', 'no spin');
    check(note(9) === '"stopping…"', 'replies still say they are stopping');
    check(status.textContent === 'Stopping 2 replies…', 'status still said');
    deliver('replies_streaming', {count: 0});
    check(!busy() && stop.hidden, 'clears');
""")


@pytest.mark.parametrize("width", [320, 1280])
def test_stop_under_reduced_motion_keeps_the_words_not_the_spin(page, tmp_path, width):
    passed(
        run_probe(
            page,
            REDUCED_PROBE,
            tmp_path,
            width,
            extra_flags=["--force-prefers-reduced-motion"],
        )
    )
