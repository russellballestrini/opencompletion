"""Fetch one page for a model, on our network, public addresses only.

The crawler itself (tools/crawler/async_web_fetcher.py) is shared
verbatim across our repos & never edited here. What this module adds is
the one thing a chat server needs that a crawler run by its operator
does not: a model picks the address, & a model reads pages, so a page
can talk it into asking for http://169.254.169.254/ or our own
localhost. Every connection, redirects included, dials only a public
address (PublicOnlyConnector).

robots.txt & crawl delays are the shared crawler's own. A site that
refuses us (robots.txt, 401/403, 429) is told so in plain words & not
asked again for REFUSAL_S, so a model can't retry its way past a no
(fox, 2026-11-02: "I don't want to ship a bad bot"). We never work
around a refusal, a bot challenge included.
"""

import asyncio
import ipaddress
import time
from urllib.parse import urlsplit

import aiohttp
import gevent

from tools.crawler import async_web_fetcher as crawler

FETCH_CHARS = 20000
REFUSAL_S = 600
# url -> (until, error); "host:<name>" for a site that asked us to slow down
_refused = {}
USER_AGENT = (
    "opencompletion/1.0 (fetches a page a person asked about; "
    "+https://github.com/russellballestrini/opencompletion)"
)


class PrivateAddressError(aiohttp.ClientConnectionError):
    """A model asked for an address that isn't on the public internet."""


def is_public(address):
    try:
        return ipaddress.ip_address(address.split("%")[0]).is_global
    except ValueError:
        return False


class PublicOnlyConnector(aiohttp.TCPConnector):
    """A connector that dials public addresses only.

    _resolve_host is where every hop of a request, redirects included,
    turns a host into the addresses we dial: a name through the resolver,
    an IP literal handed straight back. Filtering its answer covers both;
    we connect to the address we checked, so DNS rebinding gains nothing.
    It is aiohttp's own method, not public API, so
    tests/unit/test_tools.py pins that it is still the one aiohttp calls.
    """

    async def _resolve_host(self, host, port, *args, **kwargs):
        found = await super()._resolve_host(host, port, *args, **kwargs)
        public = [entry for entry in found if is_public(entry["host"])]
        if not public:
            raise PrivateAddressError(f"{host} has no public address")
        return public


class GuardedFetcher(crawler.AsyncWebFetcher):
    """The shared crawler, its one session dialing public addresses only."""

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=PublicOnlyConnector(limit=10, ttl_dns_cache=300)
            )
        return self._session


async def _fetch(url):
    fetcher = GuardedFetcher(user_agent=USER_AGENT, fast_mode=True)
    try:
        html, _links = await fetcher.fetch_webpage(url)
        if not html:
            return None
        return {
            "title": fetcher.extract_title(html),
            "text": fetcher.extract_text_content(html),
        }
    finally:
        await fetcher.aclose()


def _refusal(failure, host):
    """A site's no, in words a model can pass on, or None for a failure
    that isn't one (a timeout, a dead link)."""
    hand_over = "Don't retry; give the person the link to open themselves."
    status = failure.get("status_code")
    if failure.get("type") == "robots_txt":
        return f"robots.txt asks automated readers not to fetch this page, so we don't. {hand_over}"
    if status in (401, 403):
        return (
            f"the site refuses automated readers (HTTP {status}, often a bot "
            f"challenge); we don't get around that. {hand_over}"
        )
    if status == 429:
        return (
            f"{host} asked us to slow down (HTTP 429); nothing more from it "
            f"for 10 minutes. {hand_over}"
        )
    return None


def fetch_page(url, max_chars=FETCH_CHARS):
    """{"url", "title", "text"} for one page (text cut to max_chars), or
    {"url", "error"}. Runs in a native thread with its own event loop, so
    a slow site never holds up a gevent greenlet & two fetches never share
    a loop. Never raises."""
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"url": url, "error": "only http:// & https:// addresses"}
    host = (urlsplit(url).hostname or "").lower()
    now = time.time()
    for key in (url, "host:" + host):
        until, error = _refused.get(key, (0, None))
        if now < until:
            return {
                "url": url,
                "error": error + " (refused within 10 minutes; not asked again)",
            }
    crawler.clear_last_fetch_error()
    try:
        page = gevent.get_hub().threadpool.spawn(asyncio.run, _fetch(url)).get()
    except Exception as e:  # noqa: BLE001 — a model sees the reason
        return {"url": url, "error": str(e) or e.__class__.__name__}
    if not page:
        failure = crawler.get_last_fetch_error() or {}
        refusal = _refusal(failure, host)
        if refusal:
            _refused[url] = (now + REFUSAL_S, refusal)
            if failure.get("status_code") == 429:
                _refused["host:" + host] = (now + REFUSAL_S, refusal)
            return {"url": url, "error": refusal}
        return {
            "url": url,
            "error": failure.get("details") or "could not fetch that page",
        }
    text = page["text"]
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[cut at %d characters]" % max_chars
    return {"url": url, "title": page["title"], "text": text}
