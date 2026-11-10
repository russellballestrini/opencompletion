"""A live reply on our chat page, against what a walk of the running app
with a real streaming model found (fox, 2026-11-10): a page that never
joined its room, a stream our page fell behind until its socket dropped &
the reply ended short, a reload mid-reply that lost the reply's start, & a
reply whose header changed on reload. Run: make test-ui"""

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
    """A probe: `body` runs once our page has loaded, with check() &
    deliver(); it may await."""
    return (
        r"""
<script>
window.addEventListener('load', async () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const deliver = (event, data) => testSocket.deliver(event, data);
    const sent = (name) => testSocket.sent.filter(([event]) => event === name);
    const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
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


def with_socket(html, lands):
    """Our page with its (fake) socket's connection landing as `lands`
    says: see FAKE_SOCKET in browser.py."""
    return html.replace(
        "<head>", f"<head><script>window.TEST_SOCKET_LANDS = '{lands}';</script>", 1
    )


JOINED_PROBE = probe(r"""
    const joins = sent('join');
    check(testSocket.connected, 'our socket connected');
    check(joins.length === 1, 'joined its room once, not ' + joins.length + ' times');
    check(joins[0] && joins[0][1].room_name === 'lobby' && joins[0][1].username, 'the join names our room & who we are');
""")


def test_a_connection_that_lands_at_once_still_joins(page, tmp_path):
    """17 of 40 loads never joined: the socket was made (& connecting) in
    our page's head & its connection could land before 3-room.js attached
    the handler that joins. Now it connects once every script has loaded."""
    passed(run_probe(with_socket(page, "at once"), JOINED_PROBE, tmp_path, 1280))


def test_a_socket_already_connected_joins_all_the_same(page, tmp_path):
    """Belt & braces: connected before any handler was attached (whatever
    autoConnect said), our page still joins, once."""
    passed(run_probe(with_socket(page, "early"), JOINED_PROBE, tmp_path, 1280))


REJOIN_PROBE = probe(r"""
    check(sent('join').length === 0, 'no join before a connection lands');
    testSocket.land();
    check(sent('join').length === 1, 'joined when its connection landed');
    startChat();
    testSocket.deliver('connect');
    check(sent('join').length === 1, 'one join per connection, however often asked: ' + sent('join').length);

    // A reply streams; Stop shows.
    const stop = document.getElementById('stop-button');
    deliver('replies_streaming', {count: 1});
    check(!stop.hidden, 'Stop shows while a reply streams');
    deliver('message_chunk', {id: 9, content: 'abc', username: 'ada', model_name: 'qwen', is_first_chunk: true});
    deliver('message_chunk', {id: 10, content: 'half', username: 'ada', model_name: 'qwen', is_first_chunk: true});
    deliver('chat_message', {id: 11, username: 'bob', content: 'meanwhile'});

    // Our connection drops & comes back: we join again & our server sends
    // the room as it stands.
    testSocket.drop();
    testSocket.land();
    check(sent('join').length === 2, 'rejoined on reconnect: ' + sent('join').length);
    // Reply 10 ended while we were away: no reply streams now.
    deliver('replies_streaming', {count: 0});
    check(stop.hidden, 'a join that reports zero streaming clears Stop');
    deliver('previous_messages', {id: 9, username: 'qwen', from_model: true, content: 'abcdef', streaming: true});
    deliver('previous_messages', {id: 10, username: 'qwen', from_model: true, content: 'half & whole', streaming: false});
    deliver('previous_messages', {id: 11, username: 'bob', content: 'meanwhile', streaming: false});
    check(document.querySelectorAll('#message-9').length === 1 && document.querySelectorAll('#message-11').length === 1, 'nothing drawn twice');
    const order = [...document.querySelectorAll('#chat .message-wrapper')].map(el => el.id).join();
    check(order === 'message-9,message-10,message-11', 'messages keep their order: ' + order);
    const raw = (id) => document.querySelector('#message-' + id + ' .message-content').dataset.rawMarkdown;
    check(raw(10) === 'half & whole', 'a reply that ended while we were away is whole: ' + raw(10));
    check(document.querySelector('#message-10 .button-container'), '& has its actions');
    check(/half & whole/.test(document.querySelector('#message-10 .message-content').textContent), '& shows its whole text');
    // Reply 9 still streams: the chunks we missed are in the snapshot &
    // new ones join it.
    check(!document.querySelector('#message-9 .button-container'), 'a reply still streaming has no actions yet');
    deliver('message_chunk', {id: 9, content: 'g'});
    check(raw(9) === 'abcdefg', 'missed chunks are back & new ones join them: ' + raw(9));
    check(/abcdefg/.test(document.querySelector('#message-9 .message-content').textContent), 'on the page too');
""")


def test_reconnecting_rejoins_and_puts_right_what_was_missed(page, tmp_path):
    passed(run_probe(page, REJOIN_PROBE, tmp_path, 1280))


# 2,000 chunks of thinking, 19 ms apart in the walk; here 50 land between
# each pair of timer ticks, as they do on a page that is busy drawing.
BATCHED_PROBE = probe(r"""
    testSocket.frames = true;   // our page draws on its own frames
    const body = () => document.querySelector('#message-5 .message-thinking-body');
    let thought = '';
    let nodes = 0;
    const seen = new MutationObserver((changes) => { nodes += changes.length; });
    seen.observe(document.getElementById('chat'), {subtree: true, childList: true, characterData: true});
    for (let i = 0; i < 2000; i++) {
        const piece = 'thought ' + i + ' ';
        thought += piece;
        deliver('message_chunk', {id: 5, reasoning_content: piece});
        if (i === 0) check(!body(), 'a chunk writes nothing by itself: our next frame does');
        if (i % 50 === 49) await wait(20);
    }
    await wait(400);
    check(body() && body().textContent === thought, 'every thinking chunk is on the page, in order (' + (body() ? body().textContent.length : 'none') + ' of ' + thought.length + ' characters)');
    check(body().childNodes.length === 1 && body().firstChild.nodeType === Node.TEXT_NODE, 'thinking is one text node, appended to');
    const thinkingWrites = streamWrites;
    check(thinkingWrites > 0 && thinkingWrites <= 100, '2,000 thinking chunks took ' + thinkingWrites + ' writes');

    let answer = '';
    for (let i = 0; i < 300; i++) {
        const piece = i % 20 === 0 ? '\n\nParagraph ' + i + ' ' : 'word' + i + ' ';
        answer += piece;
        deliver('message_chunk', i === 0
            ? {id: 5, content: piece, username: 'ada', model_name: 'qwen', is_first_chunk: true}
            : {id: 5, content: piece});
        if (i % 50 === 49) await wait(20);
    }
    deliver('message_chunk', {id: 5, content: '', is_complete: true, final: answer, thinking: thought, model_name: 'qwen'});
    await wait(400);
    const content = document.querySelector('#message-5 .message-content');
    check(content.dataset.rawMarkdown === answer, 'the answer is exactly what was sent');
    check(content.querySelectorAll('p').length === 15 && /word299/.test(content.textContent), 'and rendered: ' + content.querySelectorAll('p').length + ' paragraphs');
    check(body().textContent === thought, 'thinking unchanged by the answer');
    check(document.querySelector('#message-5 .button-container'), 'complete: its actions are there');
    check(streamWrites - thinkingWrites <= 40, '300 answer chunks took ' + (streamWrites - thinkingWrites) + ' writes');
    seen.disconnect();
    check(nodes < 1500, 'far fewer DOM changes than chunks: ' + nodes);
    document.body.dataset.checks = 'writes=' + streamWrites + ' changes=' + nodes;

    // Scrolled up, a reader stays where they are; at the bottom, we follow.
    const chat = document.getElementById('chat');
    chat.scrollTop = 0;
    chat.dispatchEvent(new Event('scroll'));
    deliver('message_chunk', {id: 6, reasoning_content: 'more\n'.repeat(200)});
    await wait(300);
    check(chat.scrollTop === 0, 'a reader scrolled up is not pulled down');
    chat.scrollTop = chat.scrollHeight;
    chat.dispatchEvent(new Event('scroll'));
    deliver('message_chunk', {id: 6, reasoning_content: 'more\n'.repeat(200)});
    await wait(300);
    check(chat.scrollHeight - chat.scrollTop - chat.clientHeight < 5, 'a reader at the bottom follows the stream');
""")


def test_a_long_stream_is_drawn_once_a_frame_and_whole(page, tmp_path):
    out = run_probe(page, BATCHED_PROBE, tmp_path, 1280)
    passed(out)


WHOLE_PROBE = probe(r"""
    const content = (id) => document.querySelector('#message-' + id + ' .message-content');
    const words = (id) => content(id).textContent.replace(/\s+/g, ' ').trim();

    // A chunk never arrived (a dropped socket): the reply's end puts it right.
    deliver('message_chunk', {id: 20, reasoning_content: 'Let me '});
    deliver('message_chunk', {id: 20, content: 'Hello ', username: 'ada', model_name: 'qwen', is_first_chunk: true});
    deliver('message_chunk', {id: 20, content: 'world.'});      // 'big ' was lost on the way
    check(content(20).dataset.rawMarkdown === 'Hello world.', 'before its end: what arrived');
    deliver('message_chunk', {id: 20, content: '', is_complete: true, final: 'Hello big world.', thinking: 'Let me think.', model_name: 'qwen'});
    check(content(20).dataset.rawMarkdown === 'Hello big world.', 'a missed chunk is repaired at completion: ' + content(20).dataset.rawMarkdown);
    check(words(20) === 'Hello big world.', 'on the page too: ' + words(20));
    check(document.querySelector('#message-20 .message-thinking-body').textContent === 'Let me think.', 'its thinking too');
    check(document.querySelector('#message-20 .button-container'), 'then it is complete');
    let copied = null;
    Object.defineProperty(navigator, 'clipboard', {value: {writeText: (text) => { copied = text; return Promise.resolve(); }}, configurable: true});
    document.querySelector('#message-20 .copy-button').click();
    check(copied === 'Hello big world.', 'Copy takes the whole reply: ' + copied);

    // Repaired around its tool lines, each staying where it happened.
    deliver('message_chunk', {id: 21, content: 'Let me look.', username: 'ada', model_name: 'qwen', is_first_chunk: true});
    deliver('tool_activity', {id: 21, tool: 'sandbox_shell', label: 'ls', where: 'sandbox', asker: 'ada', model_name: 'qwen', status: 'done', at: 12});
    deliver('message_chunk', {id: 21, content: ' two'});         // ' Found' was lost
    deliver('message_chunk', {id: 21, content: ' files.'});
    deliver('message_chunk', {id: 21, content: '', is_complete: true, final: 'Let me look. Found two files.', model_name: 'qwen'});
    const flow = [...content(21).children].map(el => el.className).join();
    check(flow === 'message-part,message-tools,message-part', 'repaired flow: ' + flow);
    const parts = content(21).querySelectorAll('.message-part');
    check(parts[0].textContent.trim() === 'Let me look.' && parts[1].textContent.trim() === 'Found two files.', 'each part holds its own words: ' + parts[1].textContent);

    // A reply with every chunk is not drawn again at its end.
    deliver('message_chunk', {id: 22, content: 'All here.', username: 'ada', model_name: 'qwen', is_first_chunk: true});
    const part = content(22).querySelector('.message-part');
    deliver('message_chunk', {id: 22, content: '', is_complete: true, final: 'All here.', model_name: 'qwen'});
    check(content(22).querySelector('.message-part') === part, 'a whole reply is left as drawn');
    // An older server's end carries no text: what arrived stays.
    deliver('message_chunk', {id: 23, content: 'Old server.', username: 'ada', model_name: 'qwen', is_first_chunk: true});
    deliver('message_chunk', {id: 23, content: '', is_complete: true});
    check(content(23).dataset.rawMarkdown === 'Old server.' && document.querySelector('#message-23 .button-container'), 'an end without `final` still completes');

    // Opened (or reloaded) mid-reply: history hands us its words so far, &
    // the chunks that follow join them.
    deliver('previous_messages', {id: 30, username: 'qwen', from_model: true, streaming: true,
        content: 'The start of a long', thinking: 'Plan: ', tools: null});
    check(!document.querySelector('#message-30 .button-container'), 'no actions while it streams');
    deliver('message_chunk', {id: 30, content: ' answer'});
    deliver('message_chunk', {id: 30, content: ' ends here.'});
    check(content(30).dataset.rawMarkdown === 'The start of a long answer ends here.', 'join mid-stream then more chunks gives the full text: ' + content(30).dataset.rawMarkdown);
    check(/qwen: The start of a long answer ends here\.$/.test(words(30)), 'in one paragraph on the page: ' + words(30));
    check(document.querySelectorAll('#message-30 .message-content').length === 1, 'one reply, not two');
    deliver('message_chunk', {id: 30, content: '', is_complete: true, final: 'The start of a long answer ends here.', thinking: 'Plan: ', model_name: 'qwen'});
    copied = null;
    document.querySelector('#message-30 .copy-button').click();
    check(copied === 'The start of a long answer ends here.', 'Copy after a reload mid-reply takes it all: ' + copied);
    document.querySelector('#message-30 .edit-button').click();
    check(document.querySelector('#message-30 textarea').value === 'The start of a long answer ends here.', 'Edit too');

    // Joined while it was still thinking: thinking carries on in its block,
    // above the answer.
    deliver('previous_messages', {id: 31, username: 'qwen', from_model: true, streaming: true, content: '', thinking: 'First, ', tools: null});
    const thinking31 = document.querySelector('#message-31 .message-thinking');
    check(thinking31 && thinking31.open, 'thinking is open while that is all there is');
    deliver('message_chunk', {id: 31, reasoning_content: 'then.'});
    check(thinking31.textContent.includes('First, then.'), 'new thinking joins what history gave: ' + thinking31.textContent);
    deliver('message_chunk', {id: 31, content: 'Answer.', username: 'ada', model_name: 'qwen', is_first_chunk: true});
    check(!thinking31.open && content(31).dataset.rawMarkdown === 'Answer.', 'then its answer');
    check(thinking31.compareDocumentPosition(content(31)) & Node.DOCUMENT_POSITION_FOLLOWING, 'thinking above the answer');

    // A server that does not say `streaming` (an older one): the words
    // history drew are still kept when chunks follow.
    deliver('previous_messages', {id: 32, username: 'qwen', from_model: true, content: 'Saved so far. '});
    deliver('message_chunk', {id: 32, content: 'And more.'});
    check(content(32).dataset.rawMarkdown === 'Saved so far. And more.', 'never overwritten: ' + content(32).dataset.rawMarkdown);

    // A reply that fails arrives whole under its id: one element, not two.
    deliver('previous_messages', {id: 33, username: 'qwen', from_model: true, streaming: true, content: '', tools: null});
    deliver('chat_message', {id: 33, username: 'qwen', content: 'qwen Error: down'});
    check(document.querySelectorAll('#message-33').length === 1 && document.querySelector('#message-33 .button-container'), 'a failed reply takes its own place');
""")


def test_a_reply_ends_with_exactly_our_servers_text(page, tmp_path):
    passed(run_probe(page, WHOLE_PROBE, tmp_path, 1280))


LOOK_PROBE = probe(r"""
    // One header, live & from history: the model's name, as every sender
    // is written. Who asked is not kept with a reply.
    deliver('message_chunk', {id: 40, content: 'Live words.', username: 'walker', model_name: 'turboderp/Qwen3.8-27B-exl3', is_first_chunk: true});
    deliver('message_chunk', {id: 40, content: '', is_complete: true, final: 'Live words.', model_name: 'turboderp/Qwen3.8-27B-exl3'});
    deliver('previous_messages', {id: 41, username: 'turboderp/Qwen3.8-27B-exl3', from_model: true, content: 'Live words.'});
    const live = document.querySelector('#message-40 .message-header strong');
    const kept = document.querySelector('#message-41 .message-content strong');
    check(live && kept && live.textContent === kept.textContent, 'live & history headers say the same: ' + (live && live.textContent) + ' / ' + (kept && kept.textContent));
    check(live && live.textContent === 'turboderp/Qwen3.8-27B-exl3:', 'the model, not who asked: ' + (live && live.textContent));
    const look = (el) => { const s = getComputedStyle(el); return [s.fontSize, s.fontWeight, s.color, s.lineHeight].join(); };
    check(live && kept && look(live) === look(kept), 'and look the same: ' + (live && look(live)) + ' / ' + (kept && look(kept)));
    const top = (id, el) => Math.round(el.getBoundingClientRect().top - document.getElementById('message-' + id).getBoundingClientRect().top);
    const gap = (id, label) => {
        const words = [...document.querySelectorAll('#message-' + id + ' p')].find(p => p.textContent === 'Live words.');
        return Math.round(words.getBoundingClientRect().top - label.getBoundingClientRect().bottom);
    };
    check(Math.abs(gap(40, live) - gap(41, kept)) <= 1, 'the same space under each: ' + gap(40, live) + ' / ' + gap(41, kept));
    // A reply whose first thing is a tool has that header too.
    deliver('tool_activity', {id: 42, tool: 'offered', names: ['run_code'], status: 'done', asker: 'walker', model_name: 'qwen', at: 0});
    check(document.querySelector('#message-42 .message-header').textContent.trim() === 'qwen:', 'a tool-first reply: ' + document.querySelector('#message-42 .message-header').textContent);
    // Read aloud skips the sender, live & from history.
    check(speakableText('', 40) === 'Live words.' && speakableText('', 41) === 'Live words.', 'a header is never read aloud: ' + speakableText('', 41));

    // "Processing..." goes when a reply's first chunk shows, the oldest
    // one, & each reply still takes exactly one.
    const waiting = () => [...document.querySelectorAll('#chat [id^="message-processing-"]')].map(el => el.id.slice(-1)).join();
    deliver('chat_message', {id: 'processing-11111111111a', content: 'Processing...'});
    deliver('chat_message', {id: 'processing-11111111111b', content: 'Processing...'});
    deliver('chat_message', {id: 'processing-11111111111c', content: 'Processing...'});
    deliver('tool_activity', {id: 50, tool: 'offered', names: ['run_code'], status: 'done', asker: 'ada', model_name: 'qwen', at: 0});
    check(waiting() === 'a,b,c', 'nothing to read yet: the placeholder stays');
    deliver('message_chunk', {id: 50, reasoning_content: 'Hmm. '});
    check(waiting() === 'b,c', 'the placeholder goes at first chunk (thinking): ' + waiting());
    deliver('message_chunk', {id: 50, reasoning_content: 'More. '});
    deliver('message_chunk', {id: 50, content: 'Hi.', username: 'ada', model_name: 'qwen', is_first_chunk: true});
    check(waiting() === 'b,c', 'one reply takes one placeholder: ' + waiting());
    deliver('message_chunk', {id: 51, content: 'Yo.', username: 'ada', model_name: 'qwen', is_first_chunk: true});
    check(waiting() === 'c', 'a second reply’s first words take the next: ' + waiting());
    deliver('message_chunk', {id: 50, content: '', is_complete: true, final: 'Hi.'});
    deliver('delete_processing_message', 50);
    deliver('message_chunk', {id: 51, content: '', is_complete: true, final: 'Yo.'});
    deliver('delete_processing_message', 51);
    check(waiting() === 'c', 'their ends take no more: ' + waiting());
    // A reply that never streamed a chunk (it failed) takes its own at its end.
    deliver('delete_processing_message', 52);
    check(waiting() === '', 'a reply with no chunks clears its own at its end');

    // Thinking's summary says what a click does now.
    deliver('message_chunk', {id: 60, reasoning_content: 'Thinking hard. '});
    const thinking = document.querySelector('#message-60 .message-thinking');
    const summary = thinking.querySelector('summary');
    check(thinking.open && summary.textContent === 'thinking…', 'open while it streams: ' + summary.textContent);
    deliver('message_chunk', {id: 60, content: 'Done.', username: 'ada', model_name: 'qwen', is_first_chunk: true});
    await wait(50);
    check(!thinking.open && summary.textContent === 'thinking (click to expand)', 'collapsed under its answer: ' + summary.textContent);
    summary.click();
    await wait(50);
    check(thinking.open && !/expand/.test(summary.textContent), 'open, it no longer says click to expand: ' + summary.textContent);
    deliver('message_chunk', {id: 60, content: ' More.'});
    check(thinking.open, 'a reader who opened it keeps it open as words arrive');
    summary.click();
    await wait(50);
    check(!thinking.open && summary.textContent === 'thinking (click to expand)', 'closed again: ' + summary.textContent);
    deliver('previous_messages', {id: 61, username: 'qwen', from_model: true, content: 'Kept.', thinking: 'Old thoughts.'});
    const old = document.querySelector('#message-61 .message-thinking');
    check(!old.open && old.querySelector('summary').textContent === 'thinking (click to expand)', 'from history, collapsed');
    old.querySelector('summary').click();
    await wait(50);
    check(old.open && !/expand/.test(old.querySelector('summary').textContent), 'history’s too: ' + old.querySelector('summary').textContent);
""")


def test_a_reply_looks_alike_live_and_from_history(page, tmp_path):
    passed(run_probe(page, LOOK_PROBE, tmp_path, 1280))
