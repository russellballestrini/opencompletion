"""Headless Chromium for page tests: find a browser that launches, turn a
rendered page into a file that loads offline, run a probe inside it.

A probe is JavaScript that sets document.body.dataset.probe to "passed" or
"failed: why"; the page's DOM is dumped after the virtual time budget & the
attribute read back. No server, no network: our own /static/ files are
inlined & CDN libraries are replaced by small stubs.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_BROWSER_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    # Playwright's build, preinstalled in some containers.
    "/opt/pw-browsers/chromium",
)
_browser_cache = {}

# socket.io is swapped for a fake: `testSocket.deliver(event, data)` plays a
# server event into the page's handlers & `testSocket.sent` records emits.
FAKE_SOCKET = """<script>
window.io = () => {
    const handlers = {};
    const socket = {
        sent: [],
        on(event, fn) { (handlers[event] = handlers[event] || []).push(fn); },
        off() {},
        emit(event, data) { socket.sent.push([event, data]); },
        deliver(event, data) { (handlers[event] || []).forEach(fn => fn(data)); }
    };
    window.testSocket = socket;
    return socket;
};
</script>"""


# CI-safe launch: a hosted runner has no D-Bus session, no keyring & a
# first-run flow; without these Chromium 152 sat on --dump-dom until the
# 30 s timeout on every GitHub Actions run (2026-09-18). Our availability
# probe & every page load share this one list.
CHROME_FLAGS = [
    "--headless",
    "--disable-gpu",
    "--no-sandbox",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-crash-reporter",
    "--disable-breakpad",
    "--disable-sync",
    "--metrics-recording-only",
    "--password-store=basic",
    "--use-mock-keychain",
    "--no-proxy-server",
    "--disable-background-networking",
    "--hide-scrollbars",
]
_CHROME_ENV = dict(os.environ, DBUS_SESSION_BUS_ADDRESS="/dev/null")
_probe_failures = []


def _launches(path):
    """True when `path` dumps about:blank with our flags. A cold first
    launch on a hosted runner can take well over ten seconds (a 10 s probe
    failed every browser test on one Python 3.11 job, 2026-09-24), so each
    candidate gets two tries of 30 s in a throwaway profile."""
    for attempt in (1, 2):
        with tempfile.TemporaryDirectory() as profile:
            try:
                probe = subprocess.run(
                    [path, *CHROME_FLAGS, f"--user-data-dir={profile}"]
                    + ["--dump-dom", "about:blank"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=_CHROME_ENV,
                )
            except subprocess.TimeoutExpired:
                _probe_failures.append(f"{path} try {attempt}: timed out after 30 s")
                continue
            except OSError as error:
                _probe_failures.append(f"{path}: {error}")
                return False
        if probe.returncode == 0 and "<html" in probe.stdout.lower():
            return True
        _probe_failures.append(
            f"{path} try {attempt}: exit {probe.returncode}: {probe.stderr[-300:]}"
        )
    return False


def working_browser():
    """The first Chromium-family binary that launches headlessly, or None.
    Probed once per session: on GitHub Actions /usr/bin/chromium (a snapshot
    build) hangs before it loads any page (2026-09-18), while the runner's
    Google Chrome works, so presence on PATH is not enough."""
    if "browser" in _browser_cache:
        return _browser_cache["browser"]
    found = None
    for name in _BROWSER_CANDIDATES:
        path = shutil.which(name) or (name if os.path.isfile(name) else None)
        if path and _launches(path):
            found = path
            break
    _browser_cache["browser"] = found
    return found


def require_browser():
    browser = working_browser()
    if not browser:
        if os.environ.get("GITHUB_ACTIONS"):
            # The hosted runner ships Chrome; a skip there would let a
            # green job pass for checks that never ran. Say why it failed.
            pytest.fail(
                "no headless Chromium/Chrome launches on this runner: "
                + ("; ".join(_probe_failures) or "none found on PATH")
            )
        pytest.skip("A headless Chromium/Chrome that launches is required")
    return browser


def offline_page(html):
    """Point /static/ URLs at our files on disk so `html` runs from a file://
    URL with exactly our stylesheet, scripts & vendored libraries, and put
    a fake in place of socket.io (no server to talk to)."""
    html = re.sub(
        r'<script src="/static/vendor/socket\.io[^"]*"></script>', FAKE_SOCKET, html
    )
    static = (ROOT / "static").as_uri()
    return re.sub(r'(src|href)="/static/', rf'\1="{static}/', html)


# Runs before any page script: a blocking dialog would hang --dump-dom.
DIALOG_STUBS = """<script>
window.alert = () => {};
window.confirm = () => true;
window.prompt = (message, fallback) => fallback || 'tester';
</script>"""


def framed(html, width, height):
    """`html` inside an iframe exactly `width` CSS pixels wide.

    Headless Chrome & Chromium never lay a window out narrower than 500px
    (2026-09-24, Chromium 141), so --window-size=320 alone silently tests
    a 500px page. An iframe's own viewport is its box, so media queries,
    innerWidth & overflow inside it are those of a real 320px phone. srcdoc
    keeps the frame same-origin, letting us copy the probe's verdict out.
    """
    source = json.dumps(html).replace("</", "<\\/")
    return f"""<!DOCTYPE html><html><body style="margin:0">
<iframe id="frame" style="width:{width}px;height:{height}px;border:0;display:block"></iframe>
<script>
const frame = document.getElementById('frame');
frame.srcdoc = {source};
const poll = setInterval(() => {{
    const body = frame.contentDocument && frame.contentDocument.body;
    if (body && body.dataset.probe) {{
        document.body.dataset.probe = body.dataset.probe;
        document.body.dataset.viewport = frame.contentWindow.innerWidth;
        if (body.dataset.checks) document.body.dataset.checks = body.dataset.checks;
        clearInterval(poll);
    }}
}}, 20);
</script></body></html>"""


def run_probe(html, probe, tmp_path, width, height=900, extra_flags=()):
    """Load `html` with `probe` appended, `width` CSS pixels wide; return
    the dumped DOM, whose <body> carries the probe's data-probe verdict."""
    browser = require_browser()
    page = tmp_path / f"page-{width}.html"
    html = offline_page(html).replace("<head>", "<head>" + DIALOG_STUBS, 1)
    html = html.replace("</body>", probe + "</body>")
    page.write_text(framed(html, width, height))
    command = [
        browser,
        *CHROME_FLAGS,
        *extra_flags,
        f"--window-size={max(width, 1280) + 40},{height + 40}",
        f"--user-data-dir={tmp_path / f'browser-{width}'}",
        "--virtual-time-budget=3000",
        "--timeout=10000",
        "--dump-dom",
        page.as_uri(),
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=60, env=_CHROME_ENV
    )
    assert result.returncode == 0, result.stderr
    # The page really was laid out at `width`, not a wider fallback.
    assert f'data-viewport="{width}"' in result.stdout, result.stdout[-500:]
    return result.stdout
