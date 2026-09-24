"""Isolated auth-template checks in real Chromium; no app/database or email access.

Run: venv/bin/python -m pytest tests/functional/test_auth_ui_regressions.py
Browser cases skip if Chromium is absent. Requests are stubbed intentionally.
"""

from pathlib import Path

import pytest
from flask import Flask, render_template

from browser import run_probe  # tests/functional/browser.py

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.functional


@pytest.fixture
def auth_html():
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    with app.test_request_context():
        return render_template("auth.html")


@pytest.mark.parametrize("width", [320, 480, 1280])
def test_auth_accessibility_layout_and_flow(auth_html, tmp_path, width):
    # DOMContentLoaded ensures the template's Enter handlers are installed first.
    probe = r"""
<script>
document.addEventListener('DOMContentLoaded', async () => {
    const results = [];
    const check = (ok, name) => { if (!ok) throw new Error(name); results.push(name); };
    const visible = step => getComputedStyle(document.getElementById('auth-step-' + step)).display !== 'none';
    try {
        check(document.documentElement.lang === 'en', 'document language');
        check(visible('email') && !visible('otp') && !visible('name') && !visible('success'), 'initial visibility');
        for (const [id, autocomplete] of [['auth-email', 'email'], ['auth-otp', 'one-time-code'], ['auth-display-name', 'nickname']]) {
            const input = document.getElementById(id);
            check(input.labels.length === 1 && input.labels[0].textContent.trim(), id + ' accessible label');
            check(input.autocomplete === autocomplete, id + ' autocomplete');
        }
        check(document.getElementById('auth-otp').inputMode === 'numeric', 'numeric keyboard');
        check(document.getElementById('auth-otp').maxLength === 6, 'OTP length');
        const email = document.getElementById('auth-email');
        email.focus();
        check(document.activeElement === email, 'input takes focus');
        // :focus only matches while the window itself has focus, which a
        // headless window is not always granted: wait briefly for it, and
        // when it never arrives verify the declared ring instead of the
        // computed one (the computed check flaked ~1 in 3 runs, 2026-09-18).
        for (let i = 0; i < 20 && !document.hasFocus(); i++) await new Promise(r => setTimeout(r, 50));
        const ringOf = style => style.outlineStyle === 'solid' && style.outlineWidth === '3px';
        let ring = ringOf(getComputedStyle(email));
        if (!ring && !document.hasFocus()) {
            ring = [...document.styleSheets].flatMap(sheet => [...sheet.cssRules]).some(rule =>
                rule.selectorText && rule.selectorText.includes('input:focus') && ringOf(rule.style));
        }
        check(ring, 'input focus outline');
        check(matchMedia('(prefers-reduced-motion: reduce)').matches, 'reduced motion enabled');
        check(getComputedStyle(document.querySelector('button')).transitionDuration === '0s', 'reduced motion transition');
        const requests = [];
        let reply = {};
        window.fetch = async (url, options) => {
            requests.push([url, JSON.parse(options.body)]);
            return {ok: true, json: async () => reply};
        };
        await sendOTP();
        check(requests.length === 0 && document.getElementById('auth-email-error').textContent.includes('Please enter'), 'empty email validation');
        email.value = ' person@example.test ';
        email.dispatchEvent(new KeyboardEvent('keypress', {key: 'Enter', bubbles: true}));
        await new Promise(resolve => setTimeout(resolve, 0));
        check(visible('otp') && !visible('email'), 'Enter submits email');
        check(requests[0][0] === '/auth/send-otp' && requests[0][1].email === 'person@example.test', 'email request');
        check(document.getElementById('auth-email-display').textContent === 'person@example.test', 'email displayed');
        document.querySelector('#auth-step-otp .secondary-btn').click();
        check(visible('email') && document.getElementById('auth-email-error').textContent === '', 'back and clear errors');
        await sendOTP();
        document.getElementById('auth-otp').value = '012345';
        reply = {needs_display_name: true};
        await verifyOTP();
        check(visible('name') && requests.at(-1)[1].otp_code === '012345', 'new user OTP preserves leading zero');
        document.getElementById('auth-display-name').value = 'Example';
        reply = {user: {display_name: '<img src=x onerror=alert(1)>'}};
        await claimName();
        check(visible('success'), 'sign up success');
        check(document.getElementById('auth-success-name').children.length === 0, 'display name rendered as text');
        showAuthStep('otp');
        await verifyOTP();
        check(visible('success'), 'existing user success');
        window.fetch = async () => { throw new Error('offline'); };
        showAuthStep('email');
        await sendOTP();
        check(document.getElementById('auth-email-error').textContent === 'Network error. Please try again.', 'network error');
        for (const step of ['email', 'otp', 'name', 'success']) {
            showAuthStep(step);
            check(document.documentElement.scrollWidth <= innerWidth, step + ' no horizontal overflow');
            const card = document.querySelector('.auth-container').getBoundingClientRect();
            check(card.left >= 0 && card.right <= innerWidth, step + ' card fits viewport');
        }
        document.body.dataset.probe = 'passed';
    } catch (error) {
        document.body.dataset.probe = 'failed: ' + error.message;
    }
    document.body.dataset.checks = JSON.stringify(results);
});
</script>
"""
    out = run_probe(
        auth_html,
        probe,
        tmp_path,
        width,
        extra_flags=("--force-prefers-reduced-motion",),
    )
    assert 'data-probe="passed"' in out, out
