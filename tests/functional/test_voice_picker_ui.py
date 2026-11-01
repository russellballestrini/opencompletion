"""The voice picker sits in the chat box, left of the model, whenever our
speech service lists voices, Read replies aloud on or off (fox,
2026-11-01: hidden while it was off, Play had no voice to pick)."""

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


VOICE_PROBE = r"""
<script>
// Our speech service's voices, cached as 3-room.js caches them, so its
// DOMContentLoaded fill (after this runs) finds them; read aloud off.
localStorage.setItem('voicesList', JSON.stringify({data: [{id: 'tts-1-f5', voices: ['aria', 'clara', 'elena']}]}));
localStorage.setItem('voicesListExpiration', String(Date.now() + 60000));
localStorage.setItem('autoPlayTTS', 'false');
autoPlayTTS = false;
window.addEventListener('load', () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const voice = document.getElementById('voice-select');
    const model = document.getElementById('model-select');
    const box = voice.getBoundingClientRect();
    const info = `hidden=${voice.hidden} w=${Math.round(box.width)} h=${Math.round(box.height)} options=${voice.options.length}`;
    check(!voice.hidden && box.width >= 60 && box.height >= 20, 'it shows with read aloud off: ' + info);
    check(voice.options.length === 3, 'it lists the voices: ' + info);
    check(box.right <= model.getBoundingClientRect().left + 1, 'it sits left of the model: ' + info);
    // It looks like the model picker beside it, never a boxed dropdown
    // (fox, 2026-11-01: an old room-controls rule boxed it).
    const look = el => ['borderTopWidth', 'backgroundColor', 'fontWeight', 'fontSize', 'color']
        .map(k => getComputedStyle(el)[k]).join(' ');
    check(look(voice) === look(model), `it matches the model picker: ${look(voice)} vs ${look(model)}`);
    toggleAutoPlayTTS();
    check(!voice.hidden, 'read aloud on keeps it');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [375, 1280])
def test_voice_picker_shows_beside_the_model(client, tmp_path, width):
    html = client.get("/chat/lobby").get_data(as_text=True)
    out = run_probe(html, VOICE_PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)
