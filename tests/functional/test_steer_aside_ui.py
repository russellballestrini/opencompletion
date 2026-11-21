"""What our chat page does with a message sent mid-reply, a /btw aside & a
throttle notice (static/js/chat/5-messages.js): a steered message's one
quiet line saying where it went, an aside set apart with no actions & its
markdown sanitized, a notice with no actions, & none of them wider than a
phone. Their server side is tested elsewhere; this is our page.
Run: make test-ui"""

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
    """A probe: `body` runs once our page has loaded, with check(),
    deliver() & a few readers of what a person would see."""
    return (
        r"""
<script>
window.addEventListener('load', async () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const deliver = (event, data) => testSocket.deliver(event, data);
    const message = (id) => document.getElementById('message-' + id);
    const steers = (id) => [...message(id).querySelectorAll('.message-steer')];
    const steer = (id) => steers(id).map(el => el.textContent).join(' | ');
    const actions = (el) => [...el.querySelectorAll('button')].map(b => b.textContent.trim()).join(',');
    const fits = (el, name) => {
        const chat = document.getElementById('chat').getBoundingClientRect();
        const box = el.getBoundingClientRect();
        check(box.width > 0, name + ' is drawn');
        check(box.right <= chat.right + 1 && box.left >= chat.left - 1, name + ' stays inside our chat (' + Math.round(box.left) + '-' + Math.round(box.right) + ' in ' + Math.round(chat.left) + '-' + Math.round(chat.right) + ')');
        check(box.right <= innerWidth + 1, name + ' stays on screen (' + Math.round(box.right) + ' > ' + innerWidth + ')');
    };
    const noSidewaysScroll = () => check(document.documentElement.scrollWidth <= innerWidth, 'no sideways scroll (' + document.documentElement.scrollWidth + ' > ' + innerWidth + ')');
    try {
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


# -- a message sent while a model replies ------------------------------------

STEER_WORDS = {
    "queued": "Sent to qwen's reply in progress",
    "read": "qwen read this mid-reply",
    "answering": "qwen answers this next",
    "forwarded": "Passed to qwen's running delegate",
}

STEERED_PROBE = r"""
    deliver('chat_message', {id: 7, username: 'ada', content: 'also check the tests'});
    deliver('chat_message', {id: 8, username: 'bob', content: 'meanwhile'});
    check(steers(7).length === 0, 'no line before our server says so');
    deliver('message_steered', {message_id: 7, model_name: 'qwen', status: 'STATUS'});
    check(steers(7).length === 1, 'one line under the steered message: ' + steers(7).length);
    check(steer(7) === "WORDS", 'its words: ' + steer(7));
    check(steers(7)[0].parentElement === message(7), 'the line belongs to that message');
    check(steers(7)[0].getBoundingClientRect().top >= message(7).querySelector('.message-content').getBoundingClientRect().bottom - 1, 'under its words, not above them');
    check(steers(8).length === 0, 'no line on any other message');
    check(document.querySelectorAll('.message-steer').length === 1, 'one line on our page');
    check(/also check the tests/.test(message(7).querySelector('.message-content').textContent), 'the message keeps its words');
    check(/Delete/.test(actions(message(7))), 'the message keeps its actions: ' + actions(message(7)));
"""


@pytest.mark.parametrize("status", sorted(STEER_WORDS))
def test_a_steered_message_says_where_it_went(page, tmp_path, status):
    body = STEERED_PROBE.replace("STATUS", status).replace("WORDS", STEER_WORDS[status])
    passed(run_probe(page, probe(body), tmp_path, 1280))


UPDATED_PROBE = probe(r"""
    deliver('chat_message', {id: 7, username: 'ada', content: 'also check the tests'});
    const said = [];
    for (const status of ['queued', 'read', 'answering', 'forwarded', 'queued']) {
        deliver('message_steered', {message_id: 7, model_name: 'qwen', status});
        check(steers(7).length === 1, 'one line after ' + status + ': ' + steers(7).length);
        said.push(steer(7));
    }
    check(said.join(' / ') === "Sent to qwen's reply in progress / qwen read this mid-reply / qwen answers this next / Passed to qwen's running delegate / Sent to qwen's reply in progress", 'each status replaced the last: ' + said.join(' / '));
    const line = steers(7)[0];
    deliver('message_steered', {message_id: 7, model_name: 'hermes', status: 'read'});
    check(steers(7)[0] === line, 'the same element, rewritten');
    check(steer(7) === 'hermes read this mid-reply', 'it names the model our server named: ' + steer(7));
""")


def test_a_steer_line_is_updated_in_place(page, tmp_path):
    """Queued, then read (or answered next): one line that changes, never a
    second one beside it."""
    passed(run_probe(page, UPDATED_PROBE, tmp_path, 1280))


NOTHING_PROBE = probe(r"""
    deliver('chat_message', {id: 7, username: 'ada', content: 'also check the tests'});
    const before = document.getElementById('chat').innerHTML;
    deliver('message_steered', {message_id: 99, model_name: 'qwen', status: 'queued'});
    deliver('message_steered', {message_id: 7, model_name: 'qwen', status: 'teleported'});
    deliver('message_steered', {message_id: 7, model_name: 'qwen'});
    deliver('message_steered', {model_name: 'qwen', status: 'queued'});
    check(document.querySelectorAll('.message-steer').length === 0, 'no line drawn: ' + document.querySelectorAll('.message-steer').length);
    check(document.getElementById('chat').innerHTML === before, 'our chat is as it was');
    // An unknown status after a known one leaves that line alone.
    deliver('message_steered', {message_id: 7, model_name: 'qwen', status: 'queued'});
    deliver('message_steered', {message_id: 7, model_name: 'qwen', status: 'teleported'});
    check(steers(7).length === 1 && steer(7) === "Sent to qwen's reply in progress", 'a known line outlives an unknown status: ' + steer(7));
""")


def test_a_steer_for_nothing_on_screen_or_an_unknown_status_draws_nothing(
    page, tmp_path
):
    passed(run_probe(page, NOTHING_PROBE, tmp_path, 1280))


# `STEER_WORDS[data.status]` is a plain object lookup, so a status naming
# one of Object.prototype's own members finds a function that is no wording.
INHERITED_PROBE = probe(r"""
    deliver('chat_message', {id: 7, username: 'ada', content: 'also check the tests'});
    for (const status of ['constructor', 'toString', 'hasOwnProperty', 'valueOf']) {
        deliver('message_steered', {message_id: 7, model_name: 'qwen', status});
        check(steers(7).length === 0, status + ' drew a line: ' + steer(7));
        steers(7).forEach(el => el.remove());
    }
""")


@pytest.mark.xfail(
    strict=True,
    reason="5-messages.js reads STEER_WORDS[data.status] with no own-property "
    "check: status 'constructor', 'toString', … finds Object.prototype's "
    "function & draws a line of nonsense ('qwen', '[object Undefined]'). Our "
    "server only sends its four statuses, so nobody sees it today.",
)
def test_a_status_named_like_an_object_member_draws_nothing(page, tmp_path):
    passed(run_probe(page, INHERITED_PROBE, tmp_path, 1280))


TEXT_PROBE = probe(r"""
    deliver('chat_message', {id: 7, username: 'ada', content: 'also check the tests'});
    deliver('message_steered', {message_id: 7, model_name: '<img src=x onerror="window.pwned=1"><b>qwen</b>', status: 'read'});
    check(steers(7)[0].children.length === 0, 'a model name makes no elements');
    check(steer(7) === '<img src=x onerror="window.pwned=1"><b>qwen</b> read this mid-reply', 'it reads as written: ' + steer(7));
    deliver('message_steered', {message_id: 7, status: 'answering'});
    check(steer(7) === 'The model answers this next', 'no name: ' + steer(7));
    await new Promise(resolve => setTimeout(resolve, 50));
    check(window.pwned === undefined, 'nothing ran');
""")


def test_a_steer_line_is_text_never_markup(page, tmp_path):
    """A model's name comes from an endpoint's /models: shown as it is
    written, never read as HTML; with no name at all the line still reads."""
    passed(run_probe(page, TEXT_PROBE, tmp_path, 1280))


# -- /btw: an aside -----------------------------------------------------------

ASIDE_PROBE = probe(r"""
    deliver('chat_message', {id: 7, username: 'ada', content: 'build it'});
    deliver('chat_message', {id: 'aside-0123456789ab', username: 'ada', content: '/btw how far along is **it**?', aside: true});
    const asked = message('aside-0123456789ab');
    check(asked, 'an aside is drawn under its own id');
    check(asked.classList.contains('message-wrapper') && asked.classList.contains('is-aside'), 'set apart as an aside: ' + asked.className);
    check(!message(7).classList.contains('is-aside'), 'a stored message is no aside');
    check(asked.querySelectorAll('button').length === 0, 'an aside has no actions: ' + actions(asked));
    check(!asked.querySelector('.button-container'), 'nor an empty row of them');
    check(asked.querySelector('.message-content').dataset.rawMarkdown === undefined, 'nothing to edit or copy from');
    check(/ada/.test(asked.textContent) && /how far along is it\?/.test(asked.textContent), 'who asked & what: ' + asked.textContent);
    check(asked.querySelector('.message-content strong'), 'its markdown is rendered');

    // Its answer: a second aside, beside the first.
    deliver('chat_message', {id: 'aside-ba9876543210', username: 'qwen (aside)', content: 'Two of five files written:\n\n- `app.py`\n- `models.py`', aside: true});
    const answer = message('aside-ba9876543210');
    check(answer && answer.classList.contains('is-aside'), 'its answer is an aside too');
    check(document.body.contains(asked), 'the question is still there');
    check(document.querySelectorAll('#chat .is-aside').length === 2, 'two asides, neither replaced: ' + document.querySelectorAll('#chat .is-aside').length);
    check(/how far along/.test(asked.textContent) && /Two of five files/.test(answer.textContent), 'each keeps its own words');
    check(answer.querySelectorAll('.message-content li').length === 2 && answer.querySelector('.message-content code'), 'a list & code in an answer render');
    check(answer.querySelectorAll('button').length === 0, 'an answer has no actions: ' + actions(answer));
    const order = [...document.querySelectorAll('#chat .message-wrapper')].map(el => el.id).join();
    check(order === 'message-7,message-aside-0123456789ab,message-aside-ba9876543210', 'in the order they came: ' + order);

    // The conversation goes on around them, untouched.
    deliver('chat_message', {id: 8, username: 'bob', content: 'thanks'});
    check(/Delete/.test(actions(message(8))) && !message(8).classList.contains('is-aside'), 'a stored message after an aside is a plain one');
    check(document.querySelectorAll('#chat .is-aside').length === 2, 'both asides outlive it');
    // It looks set apart: a line down its side (style.css .is-aside).
    check(parseFloat(getComputedStyle(asked).borderLeftWidth) > 0 && getComputedStyle(asked).borderLeftStyle === 'dashed', 'a dashed line down its side');
    check(getComputedStyle(message(7)).borderLeftStyle !== 'dashed', 'which a stored message lacks');
""")


def test_an_aside_is_set_apart_with_no_actions(page, tmp_path):
    passed(run_probe(page, ASIDE_PROBE, tmp_path, 1280))


SANITIZED_PROBE = probe(r"""
    const nasty = 'before <script>window.pwned = "script"<\/script> <img src="x" onerror="window.pwned = \'onerror\'"> '
        + '<a href="javascript:window.pwned=\'href\'" onclick="window.pwned=\'click\'">link</a> '
        + '<iframe srcdoc="<script>parent.pwned=1<\/script>"></iframe> <svg onload="window.pwned=\'svg\'"></svg> '
        + '[md](javascript:window.pwned=1) **after**';
    deliver('chat_message', {id: 'aside-0123456789ab', username: 'mallory', content: '/btw ' + nasty, aside: true});
    deliver('chat_message', {id: 'aside-ba9876543210', username: 'qwen (aside)', content: nasty, aside: true});
    for (const id of ['aside-0123456789ab', 'aside-ba9876543210']) {
        const el = message(id);
        check(el && el.classList.contains('is-aside'), id + ' is drawn');
        check(!el.querySelector('script'), id + ': no script element');
        check(!el.querySelector('iframe'), id + ': no iframe');
        const handlers = [...el.querySelectorAll('*')].flatMap(node => [...node.attributes].filter(a => /^on/i.test(a.name)).map(a => node.tagName + ' ' + a.name));
        check(handlers.length === 0, id + ': no event handler attributes: ' + handlers.join());
        const scripted = [...el.querySelectorAll('[href], [src]')].filter(node => /^\s*javascript:/i.test(node.getAttribute('href') || node.getAttribute('src') || ''));
        check(scripted.length === 0, id + ': no script URLs: ' + scripted.length);
        check(/before/.test(el.textContent) && el.querySelector('strong'), id + ': its harmless words & markdown stay');
        el.querySelectorAll('a').forEach(a => { a.addEventListener('click', e => e.preventDefault()); a.click(); });
    }
    await new Promise(resolve => setTimeout(resolve, 100));
    check(window.pwned === undefined, 'nothing ran: ' + window.pwned);
""")


def test_an_aside_is_sanitized(page, tmp_path):
    """Anyone in a public chatroom may send /btw anything, & a model's
    answer is no safer: both go through renderMarkdown's sanitizer."""
    passed(run_probe(page, SANITIZED_PROBE, tmp_path, 1280))


SENDER_PROBE = probe(r"""
    deliver('chat_message', {id: 'aside-0123456789ab', username: '<img src=x onerror="window.pwned=1">mallory<script>window.pwned=2<\/script>', content: '/btw hello', aside: true});
    const el = message('aside-0123456789ab');
    check(el, 'drawn');
    check(!el.querySelector('script'), 'no script element');
    check(![...el.querySelectorAll('*')].some(node => [...node.attributes].some(a => /^on/i.test(a.name))), 'no event handler attributes');
    check(/hello/.test(el.textContent), 'its words stay');
    await new Promise(resolve => setTimeout(resolve, 100));
    check(window.pwned === undefined, 'nothing ran: ' + window.pwned);
""")


def test_an_asides_sender_name_is_sanitized_too(page, tmp_path):
    """Who asked is written above an aside from the name they joined
    under: that goes through our sanitizer with its words."""
    passed(run_probe(page, SENDER_PROBE, tmp_path, 1280))


APART_PROBE = probe(r"""
    deliver('chat_message', {id: 'aside-0123456789ab', username: 'ada', content: '/btw how far?', aside: true});
    deliver('chat_message', {id: 7, username: 'ada', content: 'also check the tests'});
    deliver('message_steered', {message_id: 7, model_name: 'qwen', status: 'queued'});
    check(steers(7).length === 1, 'the stored message has its line');
    check(!message('aside-0123456789ab').querySelector('.message-steer'), 'the aside has none');
    check(message('aside-0123456789ab').querySelectorAll('button').length === 0, 'an aside still has no actions');
""")


def test_a_steer_line_lands_on_its_message_not_an_aside_beside_it(page, tmp_path):
    passed(run_probe(page, APART_PROBE, tmp_path, 1280))


# -- a throttle notice --------------------------------------------------------

NOTICE_PROBE = probe(r"""
    deliver('chat_message', {id: 7, username: 'ada', content: 'hello'});
    deliver('chat_message', {id: 'notice-0123456789ab', content: '_Too many messages from your address. Try again in 42 seconds._'});
    const notice = message('notice-0123456789ab');
    check(notice && notice.classList.contains('message-wrapper'), 'a notice is drawn under its own id');
    check(notice.querySelectorAll('button').length === 0, 'a notice has no actions: ' + actions(notice));
    check(!notice.querySelector('.button-container'), 'nor an empty row of them');
    check(!notice.classList.contains('is-aside'), 'a notice is no aside');
    check(!notice.querySelector('.message-steer'), 'no steer line');
    const words = notice.querySelector('.message-content');
    check(words.querySelector('em') && words.querySelector('em').textContent === 'Too many messages from your address. Try again in 42 seconds.', 'its words in italics: ' + words.innerHTML);
    check(words.textContent.trim() === 'Too many messages from your address. Try again in 42 seconds.', 'nothing but its words: ' + words.textContent.trim());
    check(words.dataset.rawMarkdown === undefined, 'nothing to edit or copy from');

    // A second one (still over the limit) is its own element.
    deliver('chat_message', {id: 'notice-ba9876543210', content: '_Too many messages from your address. Try again in 40 seconds._'});
    check(document.body.contains(notice) && message('notice-ba9876543210'), 'a second notice does not replace the first');
    check(/42 seconds/.test(notice.textContent) && /40 seconds/.test(message('notice-ba9876543210').textContent), 'each keeps its own words');
    check(/Delete/.test(actions(message(7))), 'a stored message beside them keeps its actions');
""")


def test_a_throttle_notice_is_a_notice_with_no_actions(page, tmp_path):
    """Past OPENCOMPLETION_MESSAGE_LIMIT one socket gets a notice saying
    how long to wait (app.message_refused): italic words under a notice-…
    id, no sender, no actions, & each its own element."""
    passed(run_probe(page, NOTICE_PROBE, tmp_path, 1280))


# -- layout -------------------------------------------------------------------

LAYOUT_PROBE = probe(r"""
    const longWord = 'x'.repeat(400);
    const longUrl = 'https://example.com/' + 'a-very-long-path-segment/'.repeat(20) + '?q=' + 'z'.repeat(120);
    const longName = 'registry.example.com/some-organisation/' + 'a-model-with-a-very-long-name-'.repeat(8) + 'q4_k_m';
    deliver('chat_message', {id: 7, username: 'ada', content: 'also check the tests'});
    deliver('message_steered', {message_id: 7, model_name: longName, status: 'queued'});
    const line = steers(7)[0];
    check(line.textContent.length > 250, 'a long steer line');
    fits(line, 'a long steer line');
    check(line.scrollWidth <= line.clientWidth + 1, 'a long steer line wraps (' + line.scrollWidth + ' > ' + line.clientWidth + ')');
    if (innerWidth <= 320) check(line.getBoundingClientRect().height > parseFloat(getComputedStyle(line).fontSize) * 2, 'onto more lines than one');
    fits(message(7), 'the steered message');
    noSidewaysScroll();

    // Each status, long: still inside.
    for (const status of ['read', 'answering', 'forwarded']) {
        deliver('message_steered', {message_id: 7, model_name: longName, status});
        fits(steers(7)[0], 'a long ' + status + ' line');
    }
    noSidewaysScroll();

    deliver('chat_message', {id: 'aside-0123456789ab', username: 'ada', aside: true, content: '/btw ' + longWord + ' ' + longUrl + ' and then plain words that go on for a while so they wrap as words do, '.repeat(6)});
    const asked = message('aside-0123456789ab');
    fits(asked, 'a long aside');
    fits(asked.querySelector('.message-content'), "a long aside's words");
    asked.querySelectorAll('.message-content > *').forEach((el, at) => fits(el, "a long aside's block " + at));
    noSidewaysScroll();

    // An answer with what models write: a wide table, a long line of code,
    // a long link.
    deliver('chat_message', {id: 'aside-ba9876543210', username: longName + ' (aside)', aside: true, content:
        'See [' + longUrl + '](' + longUrl + ') & `' + longWord + '`\n\n'
        + '| ' + Array.from({length: 12}, (_, n) => 'column number ' + n).join(' | ') + ' |\n'
        + '|' + ' --- |'.repeat(12) + '\n'
        + '| ' + Array.from({length: 12}, (_, n) => 'value-' + 'v'.repeat(20) + n).join(' | ') + ' |\n\n'
        + '```python\nprint("' + 'y'.repeat(600) + '")\n```\n'});
    const answer = message('aside-ba9876543210');
    check(answer.querySelector('table') && answer.querySelector('pre'), 'its table & code are drawn');
    fits(answer, 'a long aside answer');
    fits(answer.querySelector('.message-content'), "a long aside answer's words");
    // Code may scroll inside its own block; its block stays inside.
    fits(answer.querySelector('pre'), "an aside's code block");
    noSidewaysScroll();
    const chat = document.getElementById('chat');
    check(chat.scrollWidth <= chat.clientWidth + 1, 'our chat itself never scrolls sideways (' + chat.scrollWidth + ' > ' + chat.clientWidth + ')');

    // A long notice too.
    deliver('chat_message', {id: 'notice-0123456789ab', content: '_Too many messages from your address. ' + 'Try again in a while. '.repeat(30) + longWord + '_'});
    fits(message('notice-0123456789ab'), 'a long notice');
    noSidewaysScroll();
    check(chat.scrollWidth <= chat.clientWidth + 1, 'nor after a long notice (' + chat.scrollWidth + ' > ' + chat.clientWidth + ')');
    // An aside keeps its side line & room inside it at every width.
    check(parseFloat(getComputedStyle(asked).paddingLeft) > 0, 'an aside keeps room beside its line');
""")


@pytest.mark.parametrize("width", [320, 1280])
def test_long_steer_lines_and_asides_fit_every_screen(page, tmp_path, width):
    passed(run_probe(page, LAYOUT_PROBE, tmp_path, width))
