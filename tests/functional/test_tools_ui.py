"""static/js/chat/8-tools.js in headless Chromium: a guest's search runs in
their browser (each vendor's answer parsed, the ring failing over) & a
tool's line inside a reply says whose network it ran on, its links safe.
Every vendor is a stubbed fetch; nothing leaves the page.
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


TOOLS_PROBE = r"""
<script>
window.addEventListener('load', async () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const deliver = (event, data) => testSocket.deliver(event, data);
    const asked = [];
    let vendors = {};
    window.fetch = (url, options) => {
        asked.push([url, options]);
        const answer = vendors[new URL(url).host];
        if (!answer) return Promise.reject(new TypeError('Failed to fetch'));
        return Promise.resolve(new Response(answer.body, {status: answer.status || 200}));
    };
    const search = async (id, query) => {
        const before = testSocket.sent.length;
        deliver('tool_request', {request_id: id, tool: 'web_search', arguments: {query: query, limit: 2}});
        for (let i = 0; i < 100 && testSocket.sent.length === before; i++) {
            await new Promise(r => setTimeout(r, 10));
        }
        const sent = testSocket.sent[testSocket.sent.length - 1];
        check(sent && sent[0] === 'tool_result' && sent[1].request_id === id, 'answers ' + id);
        return sent ? sent[1].result : {};
    };
    try {
        // Exa answers as an event stream of formatted text blocks.
        keylessCursor = 0;
        vendors = {'mcp.exa.ai': {body: 'event: message\ndata: ' + JSON.stringify({result: {content: [{type: 'text', text:
            'Title: Elmo\nURL: https://muppet.fandom.com/wiki/Elmo\nHighlights:\nA red\nmonster\n---\nTitle: Grover\nURL: https://g.org/\n---\nTitle: Oscar\nURL: https://o.org/'}]}}) + '\n\n'}};
        let found = await search('r1', 'elmo');
        check(found.success && found.served_by === 'exa', 'exa serves');
        check(found.results.length === 2, 'exa keeps the limit');
        check(found.results[0].url === 'https://muppet.fandom.com/wiki/Elmo' && found.results[0].description === 'A red monster', 'exa parsed');
        const [, exaAsk] = asked[0];
        const rpc = JSON.parse(exaAsk.body);
        check(rpc.method === 'tools/call' && rpc.params.name === 'web_search_exa' && rpc.params.arguments.query === 'elmo', 'exa asked as JSON-RPC');
        check(exaAsk.credentials === 'omit' && exaAsk.referrerPolicy === 'no-referrer', 'no cookies, no referrer');

        // A vendor that fails (a CORS block reads as one) passes to the next.
        asked.length = 0;
        keylessCursor = 0;
        vendors = {'search.parallel.ai': {body: JSON.stringify({jsonrpc: '2.0', id: 1, result: {content: [{type: 'text',
            text: JSON.stringify({results: [{url: 'https://p.org/', title: 'P', excerpts: ['one', 'two']}]})}]}})}};
        found = await search('r2', 'grover');
        check(found.served_by === 'parallel' && found.results[0].description === 'one two', 'fails over to parallel');
        check(asked.map(a => new URL(a[0]).host).join() === 'mcp.exa.ai,search.parallel.ai', 'in ring order');
        check(typeof JSON.parse(asked[1][1].body).params.arguments.session_id === 'string', 'parallel gets a session id');

        // A throttled vendor (429) passes on too; tavily asks keyless.
        asked.length = 0;
        keylessCursor = 2;
        vendors = {
            'api.tavily.com': {status: 429, body: 'slow down'},
            'api.firecrawl.dev': {body: JSON.stringify({data: {web: [{url: 'https://f.org/', title: 'F', description: 'fd'}]}})},
        };
        found = await search('r3', 'oscar');
        check(found.served_by === 'firecrawl' && found.results[0].url === 'https://f.org/', 'firecrawl after a throttle');
        check(asked[0][1].headers['X-Tavily-Access-Mode'] === 'keyless', 'tavily keyless header');

        // An isError tool answer is a failure, & with all four down we say so.
        vendors = {'mcp.exa.ai': {body: JSON.stringify({result: {isError: true, content: [{type: 'text', text: 'rate limited'}]}})}};
        keylessCursor = 0;
        found = await search('r4', 'nobody');
        check(found.success === false && /exa: rate limited/.test(found.error) && /firecrawl/.test(found.error), 'all four failing is an error');
        check(!asked.some(a => /keenable/.test(a[0])), 'keenable never asked from a browser');

        // A tool we don't run in a browser answers so, never hangs.
        deliver('tool_request', {request_id: 'r5', tool: 'web_fetch', arguments: {}});
        await new Promise(r => setTimeout(r, 20));
        const last = testSocket.sent[testSocket.sent.length - 1][1];
        check(last.request_id === 'r5' && /does not run in a browser/.test(last.result.error), 'unknown tool refused');

        // Text & tools read in the order they came, the header on top.
        deliver('message_chunk', {id: 40, content: 'Let me look.', username: 'ada', model_name: 'qwen', is_first_chunk: true});
        deliver('tool_activity', {id: 40, tool: 'sandbox_shell', label: 'ls', where: 'sandbox', asker: 'ada', model_name: 'qwen', status: 'running', at: 12});
        deliver('tool_activity', {id: 40, tool: 'sandbox_shell', label: 'ls', where: 'sandbox', asker: 'ada', model_name: 'qwen', status: 'done', at: 12});
        deliver('message_chunk', {id: 40, content: ' Found two files.'});
        const flow = [...document.querySelector('#message-40 .message-content').children].map(el => el.className);
        check(flow.join() === 'message-part,message-tools,message-part', 'live order: ' + flow.join());
        const parts40 = document.querySelectorAll('#message-40 .message-part');
        check(parts40[0].textContent.trim() === 'Let me look.' && parts40[1].textContent.trim() === 'Found two files.', 'each part holds its own words');
        check(document.querySelector('#message-40 .message-content').dataset.rawMarkdown === 'Let me look. Found two files.', 'copy & edit keep the whole reply');
        // A reply whose first thing is a tool still has its header on top.
        deliver('tool_activity', {id: 41, tool: 'offered', names: ['run_code'], status: 'done', asker: 'ada', model_name: 'qwen', at: 0});
        const body41 = document.querySelector('#message-41 .message-body');
        check(body41.firstElementChild.classList.contains('message-header') && /ada \(qwen\)/.test(body41.firstElementChild.textContent), 'header first even before any text');
        // History cuts the saved text at each line's `at`.
        deliver('previous_messages', {id: 42, username: 'qwen', content: 'Let me look. Found two files.', tools: [
            {tool: 'offered', names: ['sandbox_shell'], status: 'done', at: 0},
            {tool: 'sandbox_shell', label: 'ls', where: 'sandbox', asker: username, status: 'done', at: 12}]});
        const saved42 = [...document.querySelector('#message-42 .message-content').children];
        check(saved42.map(el => el.className).join() === 'message-part,message-tools,message-part,message-tools,message-part', 'history order: ' + saved42.map(el => el.className).join());
        check(/qwen/.test(saved42[0].textContent) && saved42[2].textContent.trim() === 'Let me look.' && saved42[4].textContent.trim() === 'Found two files.', 'history text where it was');
        // An older trail (no `at`) keeps its lines before the text.
        deliver('previous_messages', {id: 43, username: 'qwen', content: 'Old.', tools: [{tool: 'web_search', label: 'q', where: 'browser', asker: username, status: 'done'}]});
        const saved43 = [...document.querySelector('#message-43 .message-content').children].map(el => el.className);
        check(saved43.join() === 'message-part,message-tools,message-part', 'old trail order: ' + saved43.join());
        // Opened mid-reply: history drew the tool running; the live events
        // that follow finish it in place, never a second copy of the reply.
        deliver('previous_messages', {id: 50, username: 'qwen', content: 'Let me look. ', tools: [
            {tool: 'delegate', label: 'make tests pass', where: 'sandbox', asker: username, status: 'running', at: 13,
             detail: {language: '', input: 'make tests pass'}}]});
        check(document.querySelector('#message-50 .message-tool.is-running'), 'history shows the tool still running');
        deliver('tool_activity', {id: 50, tool: 'delegate', label: 'make tests pass', where: 'sandbox', asker: username, model_name: 'qwen', status: 'done', at: 13,
            detail: {language: '', input: 'make tests pass', answer: 'All green.'}});
        deliver('message_chunk', {id: 50, content: 'Done.'});
        const w50 = document.getElementById('message-50');
        check(w50.querySelectorAll('.message-content').length === 1, 'one reply, not two');
        check(w50.querySelectorAll('.message-tool').length === 1 && !w50.querySelector('.message-tool.is-running'), 'the running line finished in place');
        check(/Done\./.test(w50.querySelector('.message-content').textContent), 'new words join it');
        // A reply's first line names the tools it may call.
        deliver('tool_activity', {id: 30, tool: 'offered', names: ['web_search', 'delegate'], status: 'done', asker: username});
        const offeredLine = document.querySelector('#message-30 .message-tool');
        const chips = (el) => [...el.querySelectorAll('.message-tool-chip')].map((c) => c.textContent).join(' · ');
        check(offeredLine && offeredLine.hidden && offeredLine.classList.contains('is-offered'), 'the tools offered are kept, not drawn');
        check(getComputedStyle(offeredLine.parentElement).display === 'none', 'a list of only them takes no room');
        deliver('previous_messages', {id: 31, username: 'qwen', content: 'Hi', tools: [{tool: 'offered', names: ['run_code'], status: 'done'}]});
        check(document.querySelector('#message-31 .message-tool').hidden, 'history keeps the offered line, hidden');
        // Refreshed mid-run: history draws the running card, & live
        // updates keep updating that card, never a second one below it.
        deliver('previous_messages', {id: 51, username: 'qwen', content: 'Working. ', tools: [
            {tool: 'delegate', label: 'add parse', where: 'sandbox', asker: username, status: 'running', at: 9,
             detail: {language: '', input: 'add parse'}, progress: '[bash] git clone'}]});
        deliver('tool_activity', {id: 51, tool: 'delegate', label: 'add parse', where: 'sandbox', asker: username, model_name: 'qwen', status: 'running', at: 9,
            detail: {language: '', input: 'add parse'}, progress: '[bash] git clone\n[bash] pytest', summary: 'Cloned it; running tests.'});
        const cards51 = document.querySelectorAll('#message-51 .message-tool');
        check(cards51.length === 1, 'one card after a refresh, not ' + cards51.length);
        check(cards51[0].querySelector('.message-tool-summary').textContent === 'Cloned it; running tests.', 'the refreshed card keeps updating');
        // A tool's line inside a reply, & whose network it ran on.
        deliver('tool_activity', {id: 7, tool: 'web_search', label: 'elmo', where: 'browser', asker: username, status: 'running'});
        const line = () => document.querySelector('#message-7 .message-tool:last-child');
        check(/^Searching the web from your browser: elmo…$/.test(line().textContent), 'running line: ' + line().textContent);
        deliver('tool_activity', {id: 7, tool: 'web_search', label: '<img src=x onerror=window.pwned=1>', where: 'browser', asker: username, status: 'done', sources: [
            {url: 'https://muppet.fandom.com/', title: '<b>Elmo</b>'},
            {url: 'javascript:window.pwned=2', title: 'evil'},
        ]});
        check(document.querySelectorAll('#message-7 .message-tool').length === 1, 'done replaces running');
        const links = document.querySelectorAll('#message-7 .message-tool-sources a');
        check(links.length === 1 && links[0].href === 'https://muppet.fandom.com/' && links[0].textContent === '<b>Elmo</b>', 'sources linked, as text');
        check(links[0].rel.includes('noopener') && links[0].target === '_blank', 'sources open safely');
        check(!document.querySelector('#message-7 img, #message-7 b') && !window.pwned, 'labels never render as markup');
        deliver('tool_activity', {id: 7, tool: 'web_search', label: 'q', where: 'browser', asker: 'bob', status: 'running'});
        check(/from bob's browser/.test(line().textContent), "someone else's browser named");
        deliver('tool_activity', {id: 7, tool: 'web_search', label: 'q', where: 'browser', asker: 'bob', status: 'error', error: 'all throttled'});
        check(line().classList.contains('is-error') && /failed: all throttled/.test(line().textContent), 'errors say why');
        deliver('tool_activity', {id: 7, tool: 'web_fetch', label: 'https://a.org/', where: 'server', asker: 'fox', status: 'done', sources: []});
        check(/^Read from our server: https:\/\/a\.org\/$/.test(line().textContent), 'server line: ' + line().textContent);
        // The answer streams in under its tool lines.
        deliver('message_chunk', {id: 7, content: 'Elmo is red.', username: 'ada', model_name: 'qwen', is_first_chunk: true});
        const body = document.querySelector('#message-7 .message-body');
        const lastPart = body.querySelector('.message-content > .message-part:last-child');
        check(body.querySelector('.message-tools') && lastPart && body.querySelector('.message-tools').compareDocumentPosition(lastPart) & Node.DOCUMENT_POSITION_FOLLOWING, 'answer under the tools');
        // History: a saved reply draws its kept tool lines above its answer.
        deliver('previous_messages', {id: 8, username: 'qwen', content: 'Elmo is red.', from_model: true, tools: [
            {tool: 'web_search', label: 'elmo', where: 'browser', asker: username, status: 'done',
             sources: [{url: 'https://a.org/', title: 'A'}, {url: 'javascript:window.pwned=3', title: 'x'}]},
            {tool: 'web_search', label: 'grover', where: 'browser', asker: 'bob', status: 'error', error: 'all throttled'},
        ]});
        const saved = document.querySelectorAll('#message-8 .message-tool');
        check(saved.length === 2, 'history keeps both lines');
        check(/^Searched the web from your browser: elmo/.test(saved[0].textContent), 'history line: ' + (saved[0] || {}).textContent);
        check(saved[0].querySelectorAll('a').length === 1 && !window.pwned, 'history sources safe');
        check(/from bob's browser: grover \(failed: all throttled\)/.test(saved[1].textContent), 'history error line');
        const tools8 = document.querySelector('#message-8 .message-tools');
        const answer8 = document.querySelector('#message-8 .message-content > .message-part:last-child');
        check(tools8 && answer8 && tools8.compareDocumentPosition(answer8) & Node.DOCUMENT_POSITION_FOLLOWING && /Elmo is red/.test(answer8.textContent), 'history: answer under its tools');
        // unsandbox lines: whose machine, & files it made link to our attachments.
        deliver('tool_activity', {id: 11, tool: 'run_code', label: 'python', where: 'unsandbox', asker: username, status: 'done',
            sources: [{url: '/attachments/0123456789abcdef0123456789abcdef/plot.png', title: 'plot.png'}, {url: '/elsewhere/x', title: 'x'}]});
        const ran = document.querySelector('#message-11 .message-tool');
        check(/^Ran on unsandbox: python/.test(ran.textContent), 'run_code line: ' + ran.textContent);
        const made = ran.querySelectorAll('a');
        check(made.length === 1 && made[0].getAttribute('href') === '/attachments/0123456789abcdef0123456789abcdef/plot.png', 'artifacts linked, nothing else');
        deliver('tool_activity', {id: 11, tool: 'delegate', label: 'make tests pass', where: 'sandbox', asker: username, status: 'running'});
        const card11 = document.querySelector('#message-11 .message-tool:last-child');
        check(card11.querySelector('.tool-card-head').textContent === "uncloseai-cli is working · in this chat's sandbox" &&
            card11.querySelector('.tool-card-title').textContent === 'make tests pass', 'delegate card: ' + card11.textContent);
        // Progress updates the delegate's running line in place: one line,
        // the newest progress, & no repeat of the task above it.
        deliver('tool_activity', {id: 18, tool: 'delegate', label: 'make tests pass', where: 'sandbox', asker: username, status: 'running',
            detail: {language: '', input: 'make tests pass\nall of them'}});
        deliver('tool_activity', {id: 18, tool: 'delegate', label: 'make tests pass', where: 'sandbox', asker: username, status: 'running',
            progress: '[bash] git clone', detail: {language: '', input: 'make tests pass\nall of them'}});
        deliver('tool_activity', {id: 18, tool: 'delegate', label: 'make tests pass', where: 'sandbox', asker: username, status: 'running',
            progress: '[bash] make test', detail: {language: '', input: 'make tests pass\nall of them'}});
        // A delegate's task folds open first; the reader's choice then holds.
        check(document.querySelector('#message-18 .message-tool-detail').open, 'a delegate shows its task open first');
        document.querySelector('#message-18 .message-tool-detail').open = false;
        deliver('tool_activity', {id: 18, tool: 'delegate', label: 'make tests pass', where: 'sandbox', asker: username, status: 'running',
            progress: '[bash] make test', detail: {language: '', input: 'make tests pass\nall of them'}});
        check(!document.querySelector('#message-18 .message-tool-detail').open, 'a closed fold stays closed across updates');
        const logFold = document.querySelector('#message-18 .message-tool-log');
        check(logFold && !logFold.open && /^log · 1 line$/.test(logFold.querySelector('summary').textContent), 'the log starts closed');
        logFold.open = true;
        deliver('tool_activity', {id: 18, tool: 'delegate', label: 'make tests pass', where: 'sandbox', asker: username, status: 'running',
            progress: '[bash] make test\n↳ 45 passed', detail: {language: '', input: 'make tests pass\nall of them'}});
        check(document.querySelector('#message-18 .message-tool-log').open, 'an opened log stays open');
        // The task never changes: it sits under the title, the log at the bottom.
        const task18 = document.querySelector('#message-18 .tool-card-title + .tool-card-task');
        check(task18 && task18.querySelector('summary').textContent === 'task', 'the task fold sits under the title');
        check(document.querySelector('#message-18 .tool-card-folds .message-tool-log') && !document.querySelector('#message-18 .tool-card-folds .tool-card-task'), 'job & log fold at the bottom, the task does not');
        const w15 = document.querySelectorAll('#message-18 .message-tool');
        check(w15.length === 1, 'progress updates one delegate line, not ' + w15.length);
        check(w15[0].querySelector('.message-tool-progress').textContent === '[bash] make test\n↳ 45 passed', 'newest progress shown');
        check(!w15[0].querySelector('.message-tool-preview'), 'a delegate line does not repeat its task');
        // Vitals: one quiet row under the line, live while it runs.
        deliver('tool_activity', {id: 18, tool: 'delegate', label: 'make tests pass', where: 'sandbox', asker: username, status: 'running',
            progress: '[bash] make test', detail: {language: '', input: 'make tests pass'},
            vitals: {turn: 7, max_turns: 100, context: {used: 18234, peak: 21000, window: 82000}, tools: {total: 23, by_name: {bash: 15}},
                     activity: {label: 'in tool: bash', elapsed_s: 8.2}}});
        // The card runs from what never changes to what changes most:
        // who & what, the summary, the plan, the vitals, the folds last.
        const kids = [...document.querySelector('#message-18 .message-tool').children].map((el) => el.classList[0]);
        const at = (name) => kids.indexOf(name);
        check(at('tool-card-head') < at('tool-card-title') && at('tool-card-title') < at('message-tool-vitals') &&
            at('message-tool-vitals') < at('tool-card-folds') && at('tool-card-folds') === kids.length - 1, 'card order: ' + kids.join(','));
        const live = document.querySelector('#message-18 .message-tool-vitals');
        check(live && chips(live) === 'turn 7/100 · context 18.2k / 82.0k · 23 tool calls · in tool: bash 8s', 'live vitals: ' + (live && chips(live)));
        // uncloseai-cli's "no limit" (9001 turns) is never shown as a limit.
        deliver('tool_activity', {id: 26, tool: 'delegate', label: 'x', where: 'sandbox', asker: username, status: 'running',
            vitals: {turn: 30, max_turns: 9001, context: {}, tools: {}, activity: {}}});
        check(chips(document.querySelector('#message-26 .message-tool-vitals')) === 'turn 30', 'no huge turn limit');
        check(document.querySelector('#message-26 .message-tool').classList.contains('is-card'), 'a delegate is a card');
        // A summary is the line's first word on how it goes.
        deliver('tool_activity', {id: 21, tool: 'delegate', label: 'x', where: 'sandbox', asker: username, status: 'running',
            summary: 'Cloned ago and ran its suite: 42 pass.', progress: '[bash] make test'});
        const said = document.querySelector('#message-21 .tool-card-title + .message-tool-summary');
        check(said && said.textContent === 'Cloned ago and ran its suite: 42 pass.', 'summary right under what it was asked');
        // Play reads the human words only: never code, tool lines or folds,
        // and the glow lights those same words (fox, 2026-10-31).
        const said24 = document.createElement('div');
        said24.className = 'message-wrapper';
        said24.id = 'message-24';
        said24.innerHTML = '<div class="message-content"><p>I cloned it.</p>' +
            '<ul class="message-tools"><li class="message-tool">uncloseai-cli worked: make tests pass</li></ul>' +
            '<pre><code>make test</code></pre><p>All 45 pass. Run <code>make test</code> again.</p>' +
            '<details><summary>thinking</summary>secret plan</details></div>';
        document.getElementById('chat').appendChild(said24);
        const spoken = speakableText('', 24);
        check(/I cloned it\./.test(spoken) && /All 45 pass\./.test(spoken), 'prose spoken: ' + spoken);
        check(!/make test|uncloseai-cli|secret plan/.test(spoken), 'no code, tool line or fold spoken: ' + spoken);
        wrapSentencesForGlow(said24.querySelector('.message-content'));
        check(!said24.querySelector('pre .tts-sentence, .message-tools .tts-sentence, details .tts-sentence'), 'glow skips what is not spoken');
        check(said24.querySelectorAll('.tts-sentence').length > 0, 'glow lights the prose');
        // A wide image in a reply is never cut off: it fits the chat pane,
        // centred on it (fox, 2026-10-31: a slide ran off the right edge).
        const slideCanvas = document.createElement('canvas');
        slideCanvas.width = 2400; slideCanvas.height = 600;
        const slidePic = document.createElement('img');
        slidePic.src = slideCanvas.toDataURL();
        const slideMsg = document.createElement('div');
        slideMsg.className = 'message-wrapper';
        slideMsg.id = 'message-25';
        slideMsg.innerHTML = '<div class="message-content"><p>Slide 2</p></div>';
        slideMsg.querySelector('.message-content').appendChild(slidePic);
        document.getElementById('chat').appendChild(slideMsg);
        await new Promise((resolve) => setTimeout(resolve, 100));
        const slidePane = document.getElementById('chat').getBoundingClientRect();
        const slideBox = slidePic.getBoundingClientRect();
        check(slideBox.left >= slidePane.left - 1 && slideBox.right <= slidePane.right + 1, `wide image inside the pane (${slideBox.left}..${slideBox.right} in ${slidePane.left}..${slidePane.right})`);
        const slidePaneMid = (slidePane.left + slidePane.right) / 2, slideMid = (slideBox.left + slideBox.right) / 2;
        check(Math.abs(slidePaneMid - slideMid) < 24, `wide image centred on the pane (${slideMid} vs ${slidePaneMid})`);
        check(Math.abs(slideBox.width / slideBox.height - 4) < 0.05, 'wide image keeps its proportions');
        // The plan step leads; model trouble & time show; the job folds open.
        deliver('tool_activity', {id: 20, tool: 'delegate', label: 'x', where: 'sandbox', asker: username, status: 'running',
            vitals: {turn: 3, plan: {done: 2, total: 5, current: 'run tests'}, context: {}, tools: {}, activity: {},
                     llm: {calls: 9, retries: 2, empty: 1, last_error: 'empty_delivery'}, tokens: {input: 50000, output: 3000},
                     files: {changed: ['ago/tests/test_new.py'], count: 1}, time: {elapsed_s: 241.4, budget_s: null}}});
        const row20 = chips(document.querySelector('#message-20 .message-tool-vitals'));
        check(row20 === 'turn 3 · 2 retries · 1 empty reply', 'health chips: ' + row20);
        check(document.querySelector('#message-20 .tool-card-step').textContent === 'step 3 of 5 · run tests', 'plan step');
        const bar20 = document.querySelector('#message-20 .tool-card-bar');
        check(bar20.style.getPropertyValue('--pct') === '40%' && bar20.getAttribute('aria-valuenow') === '2', 'plan bar 2 of 5');
        check(document.querySelector('#message-20 .tool-card-clock').textContent === '4m 1s', 'the clock in the corner');
        const summaryAt = [...document.querySelector('#message-20 .message-tool').children].map((el) => el.classList[0]);
        check(summaryAt.indexOf('tool-card-plan-row') < summaryAt.indexOf('message-tool-vitals'), 'plan above vitals: ' + summaryAt.join(','));
        const job = document.querySelector('#message-20 .message-tool-job');
        check(job && job.open && /files changed \(1\)/.test(job.textContent) && /ago\/tests\/test_new\.py/.test(job.textContent), 'job fold lists files');
        check(/last error: empty_delivery/.test(job.textContent) && /50\.0k in · 3\.0k out/.test(job.textContent), 'job fold: model & tokens');
        job.open = false;
        deliver('tool_activity', {id: 20, tool: 'delegate', label: 'x', where: 'sandbox', asker: username, status: 'running',
            vitals: {turn: 4, plan: {done: 2, total: 5}, context: {}, tools: {}, activity: {}, files: {changed: [], count: 0}}});
        check(!document.querySelector('#message-20 .message-tool-job').open, 'a closed job fold stays closed');
        // The finished line keeps the delegation log in its fold.
        deliver('tool_activity', {id: 18, tool: 'delegate', label: 'make tests pass', where: 'sandbox', asker: username, status: 'done',
            detail: {language: '', input: 'make tests pass', answer: 'Done.', log: '[bash] git clone\n[bash] make test'}});
        const d18 = document.querySelectorAll('#message-18 .message-tool');
        check(d18.length === 1 && !d18[0].querySelector('.message-tool-progress'), 'finished in place, box gone');
        check(d18[0].querySelector('.is-log').textContent === '[bash] git clone\n[bash] make test', 'log kept in the fold');
        deliver('tool_activity', {id: 19, tool: 'delegate', label: 'x', where: 'sandbox', asker: username, status: 'done',
            vitals: {turn: 9, context: {used: 1, peak: 21000, window: 82000}, tools: {total: 23, by_name: {}}, activity: {}}});
        // A delegate's stderr is notes, red only when the run failed; its
        // finished log can be copied.
        deliver('tool_activity', {id: 22, tool: 'delegate', label: 'x', where: 'sandbox', asker: username, status: 'done',
            detail: {language: '', input: 't', answer: 'Done.', stderr: '(slow call: 45s)', exit_code: 0, log: '[bash] make test'}});
        check(document.querySelector('#message-22 .is-notes') && !document.querySelector('#message-22 .is-stderr'), 'a clean run: stderr as notes');
        check(document.querySelector('#message-22 .message-tool-copy'), 'the finished log has a copy button');
        deliver('tool_activity', {id: 23, tool: 'delegate', label: 'x', where: 'sandbox', asker: username, status: 'error', error: 'exited 4',
            detail: {language: '', input: 't', stderr: 'Traceback', exit_code: 4}});
        check(document.querySelector('#message-23 .is-stderr'), 'a failed run: stderr in red');
        const summary = document.querySelector('#message-19 .message-tool-vitals');
        check(summary && chips(summary) === '9 turns · peak context 21.0k · 23 tool calls', 'finished vitals: ' + (summary && chips(summary)));
        // A code tool's line folds open to its code & output, as text.
        deliver('tool_activity', {id: 12, tool: 'run_code', label: 'python', where: 'unsandbox', asker: username, status: 'done',
            detail: {language: 'python', input: 'print("<b>hi</b>")', stdout: '<b>hi</b>\n', stderr: 'oops', exit_code: 1}});
        const fold = document.querySelector('#message-12 .message-tool-detail');
        check(fold && !fold.open && fold.querySelector('summary').textContent === 'code & output', 'code folded under its line');
        check(fold.querySelector('.message-tool-input code').textContent === 'print("<b>hi</b>")', 'code shown as text');
        check(!fold.querySelector('b'), 'output never markup');
        check(fold.querySelector('.is-stderr').textContent === 'oops' && /exit code 1/.test(fold.textContent), 'stderr & exit code');
        deliver('previous_messages', {id: 13, username: 'qwen', content: 'Done.', tools: [
            {tool: 'sandbox_shell', label: 'ls', where: 'sandbox', asker: username, status: 'done', detail: {language: 'bash', input: 'ls', stdout: 'a.py'}}]});
        check(document.querySelector('#message-13 .message-tool-detail .message-tool-output').textContent === 'a.py', 'history keeps code & output');
        // The first lines of a command show without opening anything.
        deliver('tool_activity', {id: 16, tool: 'sandbox_shell', label: 'cd /root/ago', where: 'sandbox', asker: username, status: 'running',
            detail: {language: 'bash', input: 'cd /root/ago\npip install -q pytest\nmake test\necho done\nls\npwd'}});
        let preview = document.querySelector('#message-16 .message-tool-preview');
        check(preview && preview.textContent === 'cd /root/ago\npip install -q pytest\nmake test\necho done\n… 2 more lines', 'running line previews its command: ' + (preview || {}).textContent);
        const runningFold = document.querySelector('#message-16 .message-tool-detail');
        check(runningFold && runningFold.querySelector('summary').textContent === 'code', 'a running line opens to its whole code');
        check(/echo done\nls\npwd/.test(runningFold.querySelector('.message-tool-input').textContent), 'all of it, not the preview');
        deliver('tool_activity', {id: 16, tool: 'sandbox_shell', label: 'cd /root/ago', where: 'sandbox', asker: username, status: 'done',
            detail: {language: 'bash', input: 'make test', stdout: 'ok'}});
        check(document.querySelectorAll('#message-16 .message-tool-preview').length === 1 && document.querySelector('#message-16 .message-tool-detail'), 'done keeps one preview & folds the output');
        // A long line scrolls inside its block, live & in history, never the page.
        const wide = 'x = "' + 'w'.repeat(600) + '"';
        deliver('tool_activity', {id: 14, tool: 'run_code', label: 'python', where: 'unsandbox', asker: username, status: 'done',
            detail: {language: 'python', input: wide, stdout: wide}});
        deliver('message_chunk', {id: 14, content: 'Done.', username: 'ada', model_name: 'qwen', is_first_chunk: true});
        deliver('previous_messages', {id: 15, username: 'qwen', content: 'Done.', tools: [
            {tool: 'run_code', label: 'python', where: 'unsandbox', asker: username, status: 'done', detail: {language: 'python', input: wide}}]});
        for (const id of [14, 15]) {
            const fold = document.querySelector(`#message-${id} .message-tool-detail`);
            fold.open = true;
            const box = fold.querySelector('.message-tool-input');
            const chatBox = document.getElementById('chat').getBoundingClientRect();
            check(box.getBoundingClientRect().right <= chatBox.right + 1, `code stays inside the chat (${id})`);
            // The block, or highlight.js's .hljs inside it, scrolls sideways.
            const scrolls = (el) => el.scrollWidth > el.clientWidth + 1;
            check(scrolls(box) || scrolls(box.querySelector('code')), `long code scrolls in its block (${id})`);
        }
        deliver('previous_messages', {id: 9, username: 'qwen', content: 'Plain.', tools: null});
        check(!document.querySelector('#message-9 .message-tools'), 'no trail, no lines');
        check(document.documentElement.scrollWidth <= innerWidth, 'no sideways scroll');
    } catch (e) {
        problems.push('threw: ' + e);
    }
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [320, 1280])
def test_a_guest_search_runs_in_their_browser_and_shows_where(client, tmp_path, width):
    html = client.get("/chat/lobby").get_data(as_text=True)
    assert "js/chat/8-tools.js" in html
    out = run_probe(html, TOOLS_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)
