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
        check(body.querySelector('.message-tools') && body.querySelector('.message-tools').compareDocumentPosition(body.querySelector('.message-content')) & Node.DOCUMENT_POSITION_FOLLOWING, 'answer under the tools');
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
        check(tools8 && tools8.compareDocumentPosition(document.querySelector('#message-8 .message-content')) & Node.DOCUMENT_POSITION_FOLLOWING, 'history: answer under its tools');
        // unsandbox lines: whose machine, & files it made link to our attachments.
        deliver('tool_activity', {id: 11, tool: 'run_code', label: 'python', where: 'unsandbox', asker: username, status: 'done',
            sources: [{url: '/attachments/5/plot.png', title: 'plot.png'}, {url: '/elsewhere/x', title: 'x'}]});
        const ran = document.querySelector('#message-11 .message-tool');
        check(/^Ran on unsandbox: python/.test(ran.textContent), 'run_code line: ' + ran.textContent);
        const made = ran.querySelectorAll('a');
        check(made.length === 1 && made[0].getAttribute('href') === '/attachments/5/plot.png', 'artifacts linked, nothing else');
        deliver('tool_activity', {id: 11, tool: 'delegate', label: 'make tests pass', where: 'sandbox', asker: username, status: 'running'});
        check(/^uncloseai-cli is working in this chat's sandbox: make tests pass…$/.test(document.querySelector('#message-11 .message-tool:last-child').textContent), 'delegate line');
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
