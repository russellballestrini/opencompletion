"""Math in messages is typeset by KaTeX (vendored, static/vendor/): $$…$$,
\\[…\\], \\(…\\) & $…$ outside code, put in after DOMPurify (fox,
2026-11-01: a reply's LaTeX showed as raw $\\frac{1}{n}$ text)."""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from browser import run_probe  # noqa: E402  tests/functional/browser.py


@pytest.fixture
def client(test_app):
    from models import Room, db

    db.session.add(Room(name="lobby"))
    db.session.commit()
    return test_app.test_client()


MATH_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const deliver = (event, data) => testSocket.deliver(event, data);
    const reply = String.raw`The key is that the denominator factors:

$$\frac{1}{n^2 - n} = \frac{1}{n(n-1)} = \frac{1}{n-1} - \frac{1}{n}$$

The partial sum from $n=2$ to $N$ collapses: $S_N = 1 - \frac{1}{N}$, and \(N \to \infty\) gives

\[\lim_{N\to\infty} S_N = 1\]

It costs $5 and $10, a literal \$3, and code keeps its dollars: ` + "`echo $HOME $x$`" + String.raw`

` + "```python\nprint('$x$ and $$y$$')\n```";
    deliver('chat_message', {id: 7, username: 'qwen', content: reply, from_model: true});
    const msg = document.querySelector('#message-7 .message-content');
    const blocks = msg.querySelectorAll('.katex-display');
    const inline = [...msg.querySelectorAll('.katex')].filter(k => !k.closest('.katex-display'));
    check(blocks.length === 2, 'two display equations ($$ & \\[): ' + blocks.length);
    check(inline.length === 4, 'four inline ($n=2$, $N$, $S_N…$, \\(…\\)): ' + inline.length);
    check(!msg.querySelector('.katex-error'), 'every equation parses');
    check(msg.querySelector('.katex-display .mfrac'), 'a fraction is typeset, not text');
    const text = msg.textContent;
    check(text.includes('$5 and $10'), 'prices stay as written');
    check(text.includes('literal $3'), 'an escaped dollar is a dollar');
    check(msg.querySelector('code').textContent.includes('$x$'), 'inline code keeps its dollars');
    check(msg.querySelector('pre').textContent.includes("'$x$ and $$y$$'"), 'a code block keeps its dollars');
    check(!/mathx[a-z0-9]+n\d+x/.test(msg.innerHTML), 'no placeholder left behind');
    // Math never runs script: KaTeX escapes, & \href stays off.
    deliver('chat_message', {id: 8, username: 'eve', content: String.raw`$\href{javascript:alert(1)}{x}$ and $<img src=x onerror=alert(1)>$`});
    const evil = document.querySelector('#message-8 .message-content');
    check(!evil.querySelector('a[href^="javascript"], img'), 'no link or image from math');
    // A wide equation scrolls in its own line, never the page.
    deliver('chat_message', {id: 9, username: 'qwen', content: '$$' + Array(80).fill('x_{i}+').join('') + '1$$', from_model: true});
    const wide = document.querySelector('#message-9 .katex-display');
    check(wide && wide.scrollWidth > wide.clientWidth && getComputedStyle(wide).overflowX === 'auto', 'a wide equation scrolls');
    check(document.documentElement.scrollWidth <= window.innerWidth + 1, 'the page does not scroll sideways');
    // Read aloud skips math, as it skips code.
    check(!speakableText('', 7).includes('frac'), 'math is not read aloud');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [375, 1280])
def test_math_is_typeset_outside_code_and_safely(client, tmp_path, width):
    html = client.get("/chat/lobby").get_data(as_text=True)
    out = run_probe(html, MATH_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)
