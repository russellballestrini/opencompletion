"""A message's copy button: a message that is one fenced code block and
nothing else copies as its code, fences dropped (fox, 2026-11-08); anything
else copies as written."""

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


COPY_PROBE = r"""
<script>
window.addEventListener('load', () => {
    const problems = [];
    const same = (got, want, name) => { if (got !== want) problems.push(name + ': ' + JSON.stringify(got)); };
    same(soleCodeBlockText('```python\nprint(1)\nprint(2)\n```'), 'print(1)\nprint(2)', 'a lone block, with a language');
    same(soleCodeBlockText('\n```\nls -la\n```\n\n'), 'ls -la', 'blank lines around it');
    same(soleCodeBlockText('~~~\nx = 1\n~~~'), 'x = 1', 'tilde fences');
    same(soleCodeBlockText('````md\n```js\nf()\n```\n````'), '```js\nf()\n```', 'a longer fence holds a shorter one');
    same(soleCodeBlockText('Here it is:\n```\nls\n```'), null, 'words before it');
    same(soleCodeBlockText('```\na\n```\n\n```\nb\n```'), null, 'two blocks');
    same(soleCodeBlockText('```python\nstill streaming'), null, 'an unclosed fence');
    same(soleCodeBlockText('plain words'), null, 'no block');
    // The button itself writes what the helper decides.
    let written = null;
    Object.defineProperty(navigator, 'clipboard', {value: {writeText: t => { written = t; return Promise.resolve(); }}});
    copyMessageContent('```sh\necho hi\n```', null);
    same(written, 'echo hi', 'the copy button copies the code');
    copyMessageContent('see `x`', null);
    same(written, 'see `x`', 'the copy button copies other messages as written');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


def test_a_lone_code_block_copies_without_its_fences(client, tmp_path):
    html = client.get("/chat/lobby").get_data(as_text=True)
    out = run_probe(html, COPY_PROBE, tmp_path, 1280)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)
