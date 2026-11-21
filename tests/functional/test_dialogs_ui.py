"""Asking & telling in our page (askConfirm, askText & toast in utils.js),
never by a browser's own confirm, prompt or alert: those block the page,
can't be styled & hang a headless browser. Probed on /styleguide, which
shows each one. Run: make test-ui"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from browser import run_probe  # noqa: E402  tests/functional/browser.py

pytestmark = pytest.mark.functional

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def page(test_app):
    return test_app.test_client().get("/styleguide").get_data(as_text=True)


PROBE = r"""
<script>
window.addEventListener('load', async () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    window.onunhandledrejection = (e) => { document.body.dataset.probe = 'failed: ' + e.reason; };
    // A browser's own dialogs must never be asked.
    window.confirm = window.prompt = window.alert = () => { problems.push('a browser dialog was opened'); };
    const wait = (ms) => new Promise(r => setTimeout(r, ms || 20));
    const dialog = () => document.querySelector('dialog.ask-dialog');
    const button = (which) => dialog().querySelector(`[data-ask="${which}"]`);
    const inside = (el) => {
        const box = el.getBoundingClientRect();
        return box.left >= 0 && box.right <= innerWidth + 0.5 && box.top >= 0 && box.bottom <= innerHeight + 0.5;
    };
    const gone = async () => { for (let i = 0; i < 50 && dialog(); i++) await wait(); };
    const escape = () => {
        // What Escape does to a modal <dialog>: cancel, then close.
        if (dialog().dispatchEvent(new Event('cancel', {cancelable: true}))) dialog().close();
    };

    // askConfirm: opens as a modal, named & described, laid out in view.
    const opener = document.getElementById('sg-ask-confirm');
    opener.focus();
    let asked = askConfirm({title: 'Delete this chat?', body: 'This cannot be undone.', confirmLabel: 'Delete', danger: true});
    check(dialog() && dialog().open && dialog().matches(':modal'), 'a modal dialog opens');
    check(document.getElementById(dialog().getAttribute('aria-labelledby')).textContent === 'Delete this chat?', 'named by its title');
    check(document.getElementById(dialog().getAttribute('aria-describedby')).textContent === 'This cannot be undone.', 'described by its body');
    check(button('confirm').textContent === 'Delete' && button('confirm').classList.contains('btn-danger'), 'confirm says what it does, in danger red');
    check(document.activeElement === button('cancel'), 'a dangerous ask starts on Cancel');
    check(inside(dialog()) && inside(button('confirm')) && inside(button('cancel')), 'dialog & its buttons fit our screen');
    check(document.documentElement.scrollWidth <= innerWidth, 'no sideways scroll: ' + document.documentElement.scrollWidth);
    check([button('confirm'), button('cancel')].every(b => b.getBoundingClientRect().height >= 44), 'buttons are 44px tall');
    check(getComputedStyle(dialog(), '::backdrop').backgroundColor !== 'rgba(0, 0, 0, 0)', 'a backdrop dims our page');
    button('confirm').click();
    check(await asked === true, 'confirm resolves true');
    check(!dialog(), 'dialog leaves our page');
    check(document.activeElement === opener, 'focus returns to its opener');

    // Cancel, Escape & the backdrop all mean no.
    asked = askConfirm({title: 'Archive?'});
    check(document.activeElement === button('confirm') && button('confirm').textContent === 'OK', 'a safe ask starts on its confirm');
    button('cancel').click();
    check(await asked === false, 'Cancel resolves false');
    asked = askConfirm({title: 'Archive?'});
    escape();
    check(await asked === false && !dialog(), 'Escape resolves false');
    check(document.activeElement === opener, 'focus returns after Escape');
    asked = askConfirm({title: 'Archive?'});
    dialog().click();
    check(await asked === false, 'a tap on the backdrop resolves false');

    // askText: a labelled field holding its value, selected; Enter confirms.
    asked = askText({title: 'Rename this chat', label: 'Title', value: 'Old title', confirmLabel: 'Rename'});
    const field = dialog().querySelector('input.form-input');
    check(field && document.activeElement === field && field.value === 'Old title', 'its field is focused & filled');
    check(field.selectionStart === 0 && field.selectionEnd === 9, 'its value is selected, ready to replace');
    check(dialog().querySelector(`label[for="${field.id}"]`).textContent === 'Title', 'its field has a label');
    check(inside(field) && field.getBoundingClientRect().height >= 44, 'field fits & is 44px tall');
    field.value = 'New title';
    dialog().querySelector('form').requestSubmit();
    check(await asked === 'New title', 'Enter (submit) resolves what was typed');
    asked = askText({title: 'Rename this chat', value: 'Old title'});
    escape();
    check(await asked === null, 'Escape resolves null');
    asked = askText({title: 'Rename this chat', value: 'Old title'});
    button('cancel').click();
    check(await asked === null, 'Cancel resolves null');

    // Words are text, never markup.
    asked = askConfirm({title: '<img src=x onerror=alert(1)>', body: '<b>bold</b>'});
    check(!dialog().querySelector('img, b'), 'title & body are text');
    button('cancel').click();
    await asked;

    // Toasts: a status line, an alert for errors; each can be dismissed.
    const quiet = toast('Saved.');
    const loud = toast('<i>Our server said no</i>', {error: true});
    check(quiet.getAttribute('role') === 'status' && loud.getAttribute('role') === 'alert', 'status for news, alert for errors');
    check(loud.classList.contains('is-error') && !loud.querySelector('i'), 'an error is marked & its words are text');
    check(document.querySelectorAll('#toasts').length === 1 && document.querySelectorAll('#toasts .toast').length === 2, 'toasts share one stack');
    check(inside(quiet) && inside(loud), 'toasts fit our screen');
    check(getComputedStyle(document.getElementById('toasts')).position === 'fixed', 'toasts float over our page');
    check(document.documentElement.scrollWidth <= innerWidth, 'no sideways scroll with toasts');
    const dismiss = loud.querySelector('.toast-close');
    check(dismiss.getAttribute('aria-label') === 'Dismiss' && dismiss.getBoundingClientRect().height >= 40, 'Dismiss is named & tappable');
    dismiss.click();
    check(!loud.isConnected && quiet.isConnected, 'Dismiss removes its own toast');

    // Our style guide's examples really open them.
    document.getElementById('sg-ask-confirm').click();
    check(dialog() && !dialog().querySelector('input'), 'style guide opens a confirm');
    button('cancel').click();
    await gone();
    document.getElementById('sg-ask-text').click();
    check(dialog() && dialog().querySelector('input'), 'style guide opens a text ask');
    button('cancel').click();
    await gone();
    document.getElementById('sg-toast-error').click();
    check(document.querySelector('.toast.is-error'), 'style guide shows an error toast');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


@pytest.mark.parametrize("width", [320, 1280])
def test_asks_and_toasts_live_in_our_page(page, tmp_path, width):
    out = run_probe(page, PROBE, tmp_path, width)
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


# A toast leaves by itself: news after 5s, an error after 8s.
LEAVES_PROBE = r"""
<script>
window.addEventListener('load', async () => {
    const problems = [];
    const check = (ok, name) => { if (!ok) problems.push(name); };
    const quiet = toast('Saved.');
    const loud = toast('Failed.', {error: true});
    await new Promise(r => setTimeout(r, 5200));
    check(!quiet.isConnected, 'news leaves after 5s');
    check(loud.isConnected, 'an error stays longer');
    await new Promise(r => setTimeout(r, 3000));
    check(!loud.isConnected, 'an error leaves after 8s');
    document.body.dataset.probe = problems.length ? 'failed: ' + problems.join('; ') : 'passed';
});
</script>
"""


def test_a_toast_leaves_by_itself(page, tmp_path):
    out = run_probe(
        page,
        LEAVES_PROBE,
        tmp_path,
        1280,
        extra_flags=["--virtual-time-budget=10000"],
    )
    assert 'data-probe="passed"' in out, re.search(r'data-probe="[^"]*"', out)


def test_no_script_or_template_opens_a_browser_dialog():
    ours = [
        path
        for folder, pattern in (("static/js", "*.js"), ("templates", "*.html"))
        for path in (ROOT / folder).rglob(pattern)
    ]
    assert len(ours) > 20
    found = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in ours
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"(?<![.\w])(prompt|confirm|alert)\(", line)
    ]
    assert not found, found
