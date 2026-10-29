"""The four network methods, with aiohttp replaced at the session seam.

``fetch_webpage``, ``fetch_media``, ``fetch_feeds`` and
``resolve_canonical_image`` were the whole uncovered remainder of the
fetcher — together 3,022 of its 4,009 CRAP debt. Each one is mostly
policy rather than transport: cache lookups, robots checks, size
ceilings, content-type gating, timeout accounting, feed queueing,
canonical-image pattern matching. None of that needs a socket; it only
looked untestable because it sat behind ``session.get``.

So the double is a session, not a method. Everything the fetcher does
with the response stays real, including robots.txt: ``_fetch_robots_txt``
goes through the same shared session, so a route for ``/robots.txt``
exercises the real ``RobotFileParser`` and the real crawl-delay
extraction rather than a stubbed ``_can_fetch``.

This file is SHARED VERBATIM across arborist,
agents.ai.unturf.com, crawler.unturf.com and neopig, like the fetcher it
tests. The import path is the one thing that cannot be shared, so it
lives in each repo's conftest.py, bound to the name
``shared_web_fetcher``. `make ship-delta` (in crawler.unturf.com) fails
on any drift.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest


pytest.importorskip("shared_web_fetcher")

import shared_web_fetcher as mod  # noqa: E402
from shared_web_fetcher import (  # noqa: E402
    AsyncWebFetcher,
)

HOME = "https://example.com/"
ROBOTS = "https://example.com/robots.txt"


# --------------------------------------------------------------------
# The double
# --------------------------------------------------------------------


class Reply:
    """One canned HTTP response."""

    def __init__(self, body=b"", status=200, headers=None, final_uri=None):
        if isinstance(body, str):
            body = body.encode()
        self.body = body
        self.status = status
        self.headers = dict(headers or {})
        self._final_uri = final_uri
        self.requested = None

    @property
    def url(self):
        # aiohttp reports the address AFTER redirects; callers stringify it.
        return self._final_uri or self.requested

    async def text(self):
        return self.body.decode("utf-8", "replace")

    async def read(self):
        return self.body


class _Ctx:
    def __init__(self, transport, uri):
        self._transport = transport
        self._uri = uri

    async def __aenter__(self):
        outcome = self._transport.routes.get(self._uri, self._transport.default)
        if isinstance(outcome, BaseException):
            raise outcome
        outcome.requested = self._uri
        return outcome

    async def __aexit__(self, *exc):
        return False


class Transport:
    """A fake ClientSession: address -> Reply, or an exception to raise.

    ``get`` is deliberately NOT a coroutine — aiohttp's returns an object
    usable as an async context manager, and every call site here uses
    ``async with``.
    """

    def __init__(self, routes=None, default=None):
        self.routes = dict(routes or {})
        self.default = default if default is not None else Reply(status=404)
        self.requests: list = []
        self.heads: list = []
        self.closed = False

    def get(self, uri, **kw):
        self.requests.append(uri)
        return _Ctx(self, uri)

    def head(self, uri, **kw):
        self.heads.append(uri)
        return _Ctx(self, uri)

    async def close(self):
        self.closed = True

    # aiohttp's own session is an async context manager, and the sibling
    # copies use it that way (`async with aiohttp.ClientSession() as s`).
    # Entering must not mark the double closed on exit: one Transport
    # stands in for every session a single test's fetcher opens.
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# The current test's transport, for the ClientSession seam below.
_INSTALLED = {}


@pytest.fixture(autouse=True)
def _session_seam(monkeypatch):
    """Install the double for BOTH session architectures.

    The fetcher keeps ONE ClientSession for its life and hands it out
    via _get_session(), so setting ``fetcher._session`` covers most
    methods. Anything that builds its own session per call reads the
    constructor instead, so both are patched.
    """
    def _factory(*a, **kw):
        return _INSTALLED.get("transport") or Transport()

    monkeypatch.setattr(mod.aiohttp, "ClientSession", _factory)
    yield
    _INSTALLED.clear()


def wired(routes=None, *, fast_mode=True, **kw):
    """A fetcher whose every session is the returned Transport."""
    transport = Transport(routes)
    fetcher = AsyncWebFetcher(fast_mode=fast_mode, **kw)
    fetcher._session = transport         # the shared session
    _INSTALLED["transport"] = transport  # any per-call session
    return fetcher, transport


def run(coro):
    return asyncio.run(coro)


def _loc(item):
    """The located address of a media item, page or feed entry."""
    if isinstance(item, dict):
        return item.get("uri")
    return getattr(item, "uri", None)


def _canonical(result):
    """The resolved full-res address on a canonical-image result."""
    return result.get("canonical_uri")


def page(title="A Page", body="Hello world.", extra=""):
    return (f"<html><head><title>{title}</title></head>"
            f"<body><p>{body}</p>{extra}</body></html>")


def html_reply(text, **kw):
    kw.setdefault("headers", {"Content-Type": "text/html; charset=utf-8"})
    return Reply(text, **kw)


@pytest.fixture(autouse=True)
def _clear_error():
    mod.clear_last_fetch_error()
    yield
    mod.clear_last_fetch_error()


# --------------------------------------------------------------------
# fetch_webpage: the gate before the socket
# --------------------------------------------------------------------


def test_a_non_http_address_is_refused_without_a_request():
    """Scheme validation happens before anything else — a file:// or
    javascript: address must never reach the transport."""
    fetcher, transport = wired()
    html, links = run(fetcher.fetch_webpage("file:///etc/passwd"))
    assert (html, links) == (None, [])
    assert transport.requests == []


def test_a_cached_page_is_served_without_a_request():
    fetcher, transport = wired({HOME: html_reply(page())})
    run(fetcher.fetch_webpage(HOME))
    before = len(transport.requests)
    run(fetcher.fetch_webpage(HOME))
    assert len(transport.requests) == before


def test_an_external_cache_hit_short_circuits_robots_and_the_fetch():
    """The documented reason the callback exists: a cache hit skips both
    the robots round trip and the crawl delay."""
    async def _cached(uri):
        return ("<html>cached</html>", ["https://example.com/a"])

    fetcher, transport = wired({HOME: html_reply(page())})
    html, links = run(fetcher.fetch_webpage(HOME, cache_check_callback=_cached))
    assert html == "<html>cached</html>"
    assert links == ["https://example.com/a"]
    assert transport.requests == []


def test_an_external_cache_miss_falls_through_to_the_fetch():
    async def _miss(uri):
        return None

    fetcher, transport = wired({HOME: html_reply(page(body="fetched"))})
    html, _ = run(fetcher.fetch_webpage(HOME, cache_check_callback=_miss))
    assert "fetched" in html


def test_a_raising_cache_callback_does_not_lose_the_page():
    """A broken cache is a degraded cache, not a failed crawl."""
    async def _boom(uri):
        raise RuntimeError("cache exploded")

    fetcher, transport = wired({HOME: html_reply(page(body="still here"))})
    html, _ = run(fetcher.fetch_webpage(HOME, cache_check_callback=_boom))
    assert "still here" in html


def test_links_are_re_extracted_when_the_cache_has_none():
    """A page cached by an earlier link-less crawl must still be able to
    supply links, or a resumed crawl walks nowhere."""
    async def _cached(uri):
        return (page(extra='<a href="/a">a</a>'), [])

    fetcher, transport = wired()
    _, links = run(fetcher.fetch_webpage(HOME, extract_links=True,
                                         cache_check_callback=_cached))
    assert links == ["https://example.com/a"]


def test_links_are_re_extracted_from_the_in_memory_cache_too():
    fetcher, transport = wired({HOME: html_reply(page(extra='<a href="/a">a</a>'))})
    run(fetcher.fetch_webpage(HOME))                      # cached without links
    _, links = run(fetcher.fetch_webpage(HOME, extract_links=True))
    assert links == ["https://example.com/a"]


def test_a_disallowed_page_is_not_fetched():
    """robots.txt is honoured through the real parser, not a stub."""
    fetcher, transport = wired({
        ROBOTS: Reply("User-agent: *\nDisallow: /private/"),
        "https://example.com/private/x": html_reply(page()),
    })
    html, links = run(fetcher.fetch_webpage("https://example.com/private/x"))
    assert (html, links) == (None, [])
    assert mod.get_last_fetch_error()["type"] == "robots_txt"
    assert "https://example.com/private/x" not in transport.requests


def test_an_allowed_page_is_fetched_when_robots_permits_it():
    fetcher, transport = wired({
        ROBOTS: Reply("User-agent: *\nDisallow: /private/"),
        HOME: html_reply(page(body="allowed")),
    })
    html, _ = run(fetcher.fetch_webpage(HOME))
    assert "allowed" in html


# --------------------------------------------------------------------
# fetch_webpage: the response
# --------------------------------------------------------------------


def test_a_non_200_yields_nothing_and_records_the_status():
    fetcher, transport = wired({HOME: Reply(status=503)})
    assert run(fetcher.fetch_webpage(HOME)) == (None, [])
    err = mod.get_last_fetch_error()
    assert err["type"] == "http_error" and err["status_code"] == 503


def test_html_is_returned_with_its_text():
    fetcher, transport = wired({HOME: html_reply(page(body="distinctive"))})
    html, links = run(fetcher.fetch_webpage(HOME))
    assert "distinctive" in html and links == []


def test_links_are_extracted_on_request():
    fetcher, transport = wired({HOME: html_reply(page(extra='<a href="/a">a</a>'))})
    _, links = run(fetcher.fetch_webpage(HOME, extract_links=True))
    assert links == ["https://example.com/a"]


def test_anchor_text_is_extracted_on_request():
    fetcher, transport = wired({
        HOME: html_reply(page(extra='<a href="/a">Click here</a>'))})
    _, links = run(fetcher.fetch_webpage(HOME, extract_links=True,
                                         extract_anchor_text=True))
    assert links[0]["anchor_text"] == "Click here"


@pytest.mark.parametrize("mime", ["image/png", "video/mp4", "audio/mpeg"])
def test_binary_media_is_skipped_before_the_body_is_read(mime):
    """The whole point of gating on the header: an image must cost the
    headers, not the download."""
    fetcher, transport = wired({HOME: Reply(b"\x89PNG" * 1000,
                                    headers={"Content-Type": mime})})
    assert run(fetcher.fetch_webpage(HOME)) == (None, [])
    err = mod.get_last_fetch_error()
    assert err["type"] == "binary_content" and err["content_type"] == mime


def test_a_pdf_is_extracted_to_text(monkeypatch):
    monkeypatch.setattr(mod, "extract_text_from_pdf",
                        lambda b: "Extracted PDF prose.")
    fetcher, transport = wired({HOME: Reply(b"%PDF-1.4 ...",
                                    headers={"Content-Type": "application/pdf"})})
    html, _ = run(fetcher.fetch_webpage(HOME))
    assert "Extracted PDF prose." in html


def test_a_pdf_is_recognised_by_extension_when_the_header_lies(monkeypatch):
    monkeypatch.setattr(mod, "extract_text_from_pdf", lambda b: "By extension.")
    uri = "https://example.com/paper.pdf"
    fetcher, transport = wired({uri: Reply(b"%PDF-1.4",
                                   headers={"Content-Type": "text/html"})})
    html, _ = run(fetcher.fetch_webpage(uri))
    assert "By extension." in html


def test_a_pdf_title_comes_from_its_filename(monkeypatch):
    monkeypatch.setattr(mod, "extract_text_from_pdf", lambda b: "text")
    uri = "https://example.com/docs/report.pdf?v=2"
    fetcher, transport = wired({uri: Reply(b"%PDF",
                                   headers={"Content-Type": "application/pdf"})})
    html, _ = run(fetcher.fetch_webpage(uri))
    assert "<title>report.pdf</title>" in html


def test_an_unextractable_pdf_yields_nothing(monkeypatch):
    monkeypatch.setattr(mod, "extract_text_from_pdf", lambda b: None)
    fetcher, transport = wired({HOME: Reply(b"%PDF",
                                    headers={"Content-Type": "application/pdf"})})
    assert run(fetcher.fetch_webpage(HOME)) == (None, [])
    assert mod.get_last_fetch_error()["type"] == "pdf_extraction_failed"


# --------------------------------------------------------------------
# fetch_webpage: failures are classified, not just logged
# --------------------------------------------------------------------


def test_a_timeout_is_reported_as_a_timeout():
    fetcher, transport = wired({HOME: asyncio.TimeoutError()})
    assert run(fetcher.fetch_webpage(HOME)) == (None, [])
    assert mod.get_last_fetch_error()["type"] == "timeout"


def test_a_certificate_failure_is_reported_as_ssl():
    """Classification is what makes the error actionable — an operator
    fixes a certificate differently from a DNS entry."""
    fetcher, transport = wired({HOME: RuntimeError("certificate verify failed")})
    run(fetcher.fetch_webpage(HOME))
    assert mod.get_last_fetch_error()["type"] == "ssl"


def test_a_resolution_failure_is_reported_as_dns():
    fetcher, transport = wired({HOME: RuntimeError("Name or service not known")})
    run(fetcher.fetch_webpage(HOME))
    assert mod.get_last_fetch_error()["type"] == "dns"


def test_any_other_failure_is_reported_as_unknown():
    fetcher, transport = wired({HOME: RuntimeError("something else entirely")})
    run(fetcher.fetch_webpage(HOME))
    assert mod.get_last_fetch_error()["type"] == "unknown"


# --------------------------------------------------------------------
# robots.txt handling
# --------------------------------------------------------------------


def test_robots_txt_is_fetched_once_per_domain():
    fetcher, transport = wired({
        ROBOTS: Reply("User-agent: *\nAllow: /"),
        HOME: html_reply(page()),
        "https://example.com/a": html_reply(page()),
    })
    run(fetcher.fetch_webpage(HOME))
    run(fetcher.fetch_webpage("https://example.com/a"))
    assert transport.requests.count(ROBOTS) == 1


def test_a_missing_robots_txt_permits_the_crawl():
    """404 means no rules, not "deny"."""
    fetcher, transport = wired({HOME: html_reply(page(body="no rules here"))})
    html, _ = run(fetcher.fetch_webpage(HOME))
    assert "no rules here" in html


def test_a_declared_crawl_delay_is_recorded():
    fetcher, transport = wired({ROBOTS: Reply("User-agent: *\nCrawl-delay: 7")},
                       fast_mode=False)
    run(fetcher._can_fetch(HOME))
    assert fetcher.domain_crawl_delays["example.com"] == 7


def test_fast_mode_zeroes_a_declared_crawl_delay():
    """--fast contracts to ignore crawl-delay; a site declaring 7s must
    not stall a fast wave."""
    fetcher, transport = wired({ROBOTS: Reply("User-agent: *\nCrawl-delay: 7")},
                       fast_mode=True)
    run(fetcher._can_fetch(HOME))
    assert fetcher.domain_crawl_delays["example.com"] == 0.0


def test_an_unreachable_robots_txt_permits_the_crawl():
    fetcher, transport = wired({ROBOTS: RuntimeError("connection reset"),
                        HOME: html_reply(page(body="still allowed"))})
    html, _ = run(fetcher.fetch_webpage(HOME))
    assert "still allowed" in html


# --------------------------------------------------------------------
# fetch_media
# --------------------------------------------------------------------


IMG = "https://example.com/i/photo.jpg"


def test_media_is_returned_with_its_hash_and_size():
    payload = b"\xff\xd8\xff" + b"pixels" * 100
    fetcher, transport = wired({IMG: Reply(payload,
                                   headers={"Content-Type": "image/jpeg"})})
    got = run(fetcher.fetch_media(IMG, session=transport))
    assert got["md5_hash"] == hashlib.md5(payload).hexdigest()
    assert got["size"] == len(payload)
    assert got["data"] == payload


def test_media_carries_its_mime_and_media_type():
    fetcher, transport = wired({IMG: Reply(b"x", headers={"Content-Type":
                                                  "image/jpeg; charset=binary"})})
    got = run(fetcher.fetch_media(IMG, session=transport))
    assert got["mime_type"] == "image/jpeg"
    assert got["media_type"] == "image"


def test_media_reports_the_address_after_redirects():
    """Callers store this as the item's identity; the pre-redirect
    address would key the same object under two names."""
    final = "https://cdn.example.com/i/photo.jpg"
    fetcher, transport = wired({IMG: Reply(b"x", headers={"Content-Type": "image/jpeg"},
                                   final_uri=final)})
    got = run(fetcher.fetch_media(IMG, session=transport))
    assert _loc(got) == final


def test_media_from_a_skipped_domain_is_not_requested():
    fetcher, transport = wired({IMG: Reply(b"x")})
    fetcher.skip_domains.add("example.com")
    assert run(fetcher.fetch_media(IMG, session=transport)) is None
    assert transport.requests == []
    assert mod.get_last_fetch_error()["type"] == "skip_domain"


def test_media_disallowed_by_robots_is_not_fetched():
    fetcher, transport = wired({ROBOTS: Reply("User-agent: *\nDisallow: /i/"),
                        IMG: Reply(b"x")})
    assert run(fetcher.fetch_media(IMG, session=transport)) is None
    assert mod.get_last_fetch_error()["type"] == "robots_txt"


def test_a_non_200_media_response_yields_nothing():
    fetcher, transport = wired({IMG: Reply(status=404)})
    assert run(fetcher.fetch_media(IMG, session=transport)) is None
    assert mod.get_last_fetch_error()["type"] == "http_error"


def test_a_declared_oversize_is_refused_before_downloading():
    """Content-Length is checked first precisely so the bytes are never
    pulled — the ceiling is a cost bound, not a post-hoc filter."""
    fetcher, transport = wired({IMG: Reply(b"x" * 10,
                                   headers={"Content-Length": "999999999"})})
    assert run(fetcher.fetch_media(IMG, session=transport,
                                   max_size=1000)) is None
    assert mod.get_last_fetch_error()["type"] == "too_large"


def test_an_undeclared_oversize_is_refused_after_downloading():
    """A server that omits Content-Length must not defeat the ceiling."""
    fetcher, transport = wired({IMG: Reply(b"x" * 5000)})
    assert run(fetcher.fetch_media(IMG, session=transport,
                                   max_size=1000)) is None
    assert mod.get_last_fetch_error()["type"] == "too_large"


def test_a_media_timeout_yields_nothing_and_is_recorded():
    fetcher, transport = wired({IMG: asyncio.TimeoutError()})
    assert run(fetcher.fetch_media(IMG, session=transport)) is None
    assert mod.get_last_fetch_error()["type"] == "timeout"
    assert fetcher.domain_timeout_counts["example.com"] == 1


def test_a_domain_is_skipped_after_repeated_timeouts():
    """The circuit breaker. A dead host must stop costing the crawl a
    full timeout per item."""
    fetcher, transport = wired({IMG: asyncio.TimeoutError()})
    for _ in range(fetcher.MAX_CONSECUTIVE_TIMEOUTS):
        run(fetcher.fetch_media(IMG, session=transport))
    assert "example.com" in fetcher.skip_domains


def test_a_success_resets_the_timeout_count():
    """Consecutive is the word in the constant's name — an intervening
    success must clear the tally or a slow host eventually trips."""
    fetcher, transport = wired({IMG: asyncio.TimeoutError()})
    run(fetcher.fetch_media(IMG, session=transport))
    assert fetcher.domain_timeout_counts["example.com"] == 1
    transport.routes[IMG] = Reply(b"ok", headers={"Content-Type": "image/jpeg"})
    run(fetcher.fetch_media(IMG, session=transport))
    assert fetcher.domain_timeout_counts["example.com"] == 0


def test_media_builds_and_closes_its_own_session_when_given_none(monkeypatch):
    """The ownership rule: a session the method created is a session the
    method must close, or a long crawl leaks sockets."""
    transport = Transport({IMG: Reply(b"x", headers={"Content-Type": "image/jpeg"})})
    monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda *a, **k: transport)
    fetcher = AsyncWebFetcher(fast_mode=True)
    fetcher._session = transport
    assert run(fetcher.fetch_media(IMG)) is not None
    assert transport.closed


# --------------------------------------------------------------------
# fetch_feeds
# --------------------------------------------------------------------


RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
  <item><title>One</title><link>https://example.com/one</link></item>
  <item><title>Two</title><link>https://example.com/two</link></item>
</channel></rss>"""

SITEMAP_INDEX = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-a.xml</loc></sitemap>
</sitemapindex>"""

SITEMAP_A = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/deep</loc></url>
</urlset>"""


def _locs(items):
    return [_loc(i) for i in items]


def feeds(fetcher, base, **kw):
    """fetch_feeds with the known-feeds keyword spelled for this copy."""
    if "known" in kw:
        kw["feed_uris"] = kw.pop("known")
    return run(fetcher.fetch_feeds(base, **kw))


def test_known_feeds_are_fetched_directly():
    feed = "https://example.com/feed.xml"
    fetcher, transport = wired({feed: Reply(RSS)})
    items = feeds(fetcher, HOME, known=[feed])
    assert _locs(items) == ["https://example.com/one", "https://example.com/two"]


def test_feeds_are_discovered_from_supplied_html():
    feed = "https://example.com/feed.xml"
    html = ('<html><head><link rel="alternate" type="application/rss+xml" '
            f'href="{feed}"></head><body></body></html>')
    fetcher, transport = wired({feed: Reply(RSS)})
    items = feeds(fetcher, HOME, html=html)
    assert len(items) == 2
    assert feed in fetcher._last_discovered_feeds


def test_common_locations_are_probed_when_nothing_is_known():
    """With no html and no known feeds the fetcher guesses the standard
    addresses — that guess is what makes hydra mode work on a cold
    domain."""
    fetcher, transport = wired({"https://example.com/sitemap.xml": Reply(SITEMAP_A)})
    items = feeds(fetcher, HOME)
    assert _locs(items) == ["https://example.com/deep"]
    assert "https://example.com/sitemap.xml" in transport.requests


def test_a_working_probed_feed_is_remembered():
    fetcher, transport = wired({"https://example.com/sitemap.xml": Reply(SITEMAP_A)})
    feeds(fetcher, HOME)
    assert "https://example.com/sitemap.xml" in fetcher._last_discovered_feeds


def test_a_nested_sitemap_is_followed():
    fetcher, transport = wired({"https://example.com/sitemap.xml": Reply(SITEMAP_INDEX),
                        "https://example.com/sitemap-a.xml": Reply(SITEMAP_A)})
    items = feeds(fetcher, HOME)
    assert _locs(items) == ["https://example.com/deep"]


def test_a_feed_is_never_fetched_twice():
    feed = "https://example.com/feed.xml"
    fetcher, transport = wired({feed: Reply(RSS)})
    feeds(fetcher, HOME, known=[feed, feed, feed])
    assert transport.requests.count(feed) == 1


def test_the_sitemap_ceiling_is_respected():
    """Sitemap indexes can nest without bound; max_sitemaps is the only
    thing standing between a crawl and a fork bomb."""
    routes = {f"https://example.com/s{i}.xml": Reply(SITEMAP_A) for i in range(20)}
    fetcher, transport = wired(routes)
    feeds(fetcher, HOME, known=list(routes), max_sitemaps=3)
    assert len(transport.requests) <= 3


def test_a_missing_feed_does_not_end_the_run():
    good = "https://example.com/good.xml"
    fetcher, transport = wired({good: Reply(RSS)})
    items = feeds(fetcher, HOME, known=["https://example.com/gone.xml", good])
    assert len(items) == 2


def test_a_failing_feed_does_not_end_the_run():
    good = "https://example.com/good.xml"
    fetcher, transport = wired({"https://example.com/bad.xml": RuntimeError("reset"),
                        good: Reply(RSS)})
    items = feeds(fetcher, HOME, known=["https://example.com/bad.xml", good])
    assert len(items) == 2


def test_a_feed_timeout_does_not_end_the_run():
    good = "https://example.com/good.xml"
    fetcher, transport = wired({"https://example.com/slow.xml": asyncio.TimeoutError(),
                        good: Reply(RSS)})
    items = feeds(fetcher, HOME, known=["https://example.com/slow.xml", good])
    assert len(items) == 2


def test_a_plain_fetcher_has_no_session_attribute():
    """The premise of the test below, stated on its own.

    fetch_feeds used to read `self.session`. No constructor sets it, so
    every iteration raised AttributeError into the per-feed except, was
    logged at debug, and the method returned [] — feed and sitemap
    discovery was silently dead for every caller."""
    assert not hasattr(AsyncWebFetcher(fast_mode=True), "session")


def test_feeds_work_on_a_fetcher_nobody_configured():
    """So fetch_feeds has to obtain a session the same way every other
    method in this class does. Nothing in this file hands it one by
    hand: `wired` deliberately does not set that attribute."""
    feed = "https://example.com/feed.xml"
    fetcher, _ = wired({feed: Reply(RSS)})
    assert not hasattr(fetcher, "session")
    assert len(feeds(fetcher, HOME, known=[feed])) == 2


# --------------------------------------------------------------------
# resolve_canonical_image
# --------------------------------------------------------------------


DETAIL = "https://example.com/media/42/details"
THUMB = "https://example.com/t/42.jpg"


def resolve(html, detail=DETAIL, thumb=THUMB, title=None, extra_routes=None):
    # Wrapped in a real document: BeautifulSoup's html.parser does not
    # synthesize a <body> around a bare fragment the way a browser (or
    # lxml) would, and the body-text extraction keys on finding one.
    routes = {detail: html_reply(f"<html><body>{html}</body></html>")}
    routes.update(extra_routes or {})
    fetcher, transport = wired(routes)
    return run(fetcher.resolve_canonical_image(detail, thumb, title,
                                               session=transport))


def test_a_rest_style_parent_image_is_canonical():
    """Pattern 1: /media/42/details showing /media/42 — the detail page
    is a view OF the resource, so the resource itself is the full-res."""
    got = resolve('<img src="https://example.com/media/42">')
    assert _canonical(got) == "https://example.com/media/42"


def test_a_versioned_wrapped_link_is_canonical():
    """Pattern 2: an <a> carrying query params around a thumbnail is the
    cache-busted full-size."""
    got = resolve('<a href="/full/42.jpg?v=9"><img src="/t/42.jpg"></a>')
    assert _canonical(got) == "https://example.com/full/42.jpg?v=9"


def test_a_same_domain_query_endpoint_is_canonical():
    """Same shape but no file extension — a render endpoint still counts
    when it is on our own host."""
    got = resolve('<a href="/render?id=42"><img src="/t/42.jpg"></a>')
    assert _canonical(got) == "https://example.com/render?id=42"


def test_a_download_link_is_canonical():
    """Pattern 3: <a download> is semantic HTML saying "this is the
    file"."""
    got = resolve('<a download href="/files/42-full.jpg">Download</a>')
    assert _canonical(got) == "https://example.com/files/42-full.jpg"


def test_an_image_sharing_the_path_is_canonical():
    """Pattern 4: same host, ≥2 shared path segments."""
    got = resolve('<img src="/media/42/full.jpg">')
    assert _canonical(got) == "https://example.com/media/42/full.jpg"


def test_og_image_is_the_last_resort():
    """Pattern 5. It is last because it is the least specific claim on
    the page — a site-wide banner also lives there."""
    got = resolve('<meta property="og:image" content="https://cdn.x/og.png">')
    assert _canonical(got) == "https://cdn.x/og.png"


def test_a_relative_og_image_is_resolved_against_the_page():
    """Patterns 1-4 all urljoin their result; pattern 5 alone did not,
    so a relative og:image (common, despite the spec) became a
    canonical_uri no caller could fetch."""
    got = resolve('<meta property="og:image" content="/og/42.png">')
    assert _canonical(got) == "https://example.com/og/42.png"


def test_a_relative_og_image_matching_the_thumbnail_is_refused():
    """The thumbnail check has to happen AFTER resolution or the
    relative spelling of the same image walks straight through it."""
    got = resolve('<meta property="og:image" content="/t/42.jpg">')
    assert _canonical(got) is None


def test_the_thumbnail_is_never_returned_as_its_own_canonical():
    """Returning the thumbnail would silently make the whole resolution
    a no-op that looks like a success."""
    got = resolve(f'<meta property="og:image" content="{THUMB}">'
                  f'<img src="{THUMB}">')
    assert _canonical(got) != THUMB


def test_a_page_with_no_candidates_resolves_to_nothing():
    got = resolve("<p>Nothing here.</p>")
    assert got is not None and _canonical(got) is None


def test_the_listing_title_wins_over_the_detail_page_title():
    """The title from the page that LINKED here describes the image;
    the detail page's own title often describes the site."""
    got = resolve("<title>Site Name - Gallery</title>", title="A Red Balloon")
    assert got["title"] == "A Red Balloon"
    assert got["detail_title"] == "Site Name - Gallery"


@pytest.mark.parametrize("html,expected", [
    ('<meta property="og:title" content="From OG">', "From OG"),
    ('<img src="/x.jpg" alt="From Alt">', "From Alt"),
    ("<h1>From H1</h1>", "From H1"),
    ("<title>From Title</title>", "From Title"),
])
def test_the_detail_title_falls_back_through_four_rungs(html, expected):
    assert resolve(html)["detail_title"] == expected


def test_a_one_character_alt_is_not_a_title():
    """Spacer images carry alt="" or alt="." — those are layout, not a
    name."""
    got = resolve('<img src="/x.jpg" alt="."><h1>Real Name</h1>')
    assert got["detail_title"] == "Real Name"


def test_the_description_comes_from_og_first():
    got = resolve('<meta property="og:description" content="OG words">'
                  '<meta name="description" content="Meta words">')
    assert got["description"] == "OG words"


def test_the_description_falls_back_to_the_meta_tag():
    got = resolve('<meta name="description" content="Meta words">')
    assert got["description"] == "Meta words"


def test_the_body_text_is_captured_for_search():
    got = resolve("<p>A distinctive sentence about the artwork.</p>")
    assert "distinctive sentence" in got["detail_content"]


def test_chrome_is_stripped_from_the_body_text():
    """Nav and footer text is the same on every page of a site; leaving
    it in makes every detail page look alike to a search index."""
    got = resolve("<nav>Home About Contact</nav>"
                  "<p>The actual caption.</p>"
                  "<footer>Copyright 2026</footer>")
    assert "actual caption" in got["detail_content"]
    assert "Copyright" not in got["detail_content"]
    assert "About" not in got["detail_content"]


def test_a_disallowed_detail_page_is_not_fetched():
    fetcher, transport = wired({
        ROBOTS: Reply("User-agent: *\nDisallow: /media/"),
        DETAIL: html_reply("<img src='/media/42/full.jpg'>"),
    })
    got = run(fetcher.resolve_canonical_image(DETAIL, THUMB, session=transport))
    assert got is None
    assert DETAIL not in transport.requests


def test_a_non_200_detail_page_resolves_to_nothing():
    fetcher, transport = wired({DETAIL: Reply(status=500)})
    assert run(fetcher.resolve_canonical_image(
        DETAIL, THUMB, session=transport)) is None


def test_a_failing_detail_fetch_resolves_to_nothing():
    fetcher, transport = wired({DETAIL: RuntimeError("connection reset")})
    assert run(fetcher.resolve_canonical_image(
        DETAIL, THUMB, session=transport)) is None


def test_resolution_builds_and_closes_its_own_session_when_given_none(monkeypatch):
    transport = Transport({DETAIL: html_reply('<img src="/media/42/full.jpg">')})
    monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda *a, **k: transport)
    fetcher = AsyncWebFetcher(fast_mode=True)
    fetcher._session = transport
    assert run(fetcher.resolve_canonical_image(DETAIL, THUMB)) is not None
    assert transport.closed


# --------------------------------------------------------------------
# check_media_uri: classifying an extensionless address
# --------------------------------------------------------------------


BARE = "https://example.com/asset/9182"

def test_an_extensionless_address_is_classified_by_its_mime_type():
    """The whole reason this method exists: /asset/9182 carries no
    extension, so only the server can say what it is."""
    fetcher, transport = wired({BARE: Reply(headers={"Content-Type": "image/png"})})
    got = run(fetcher.check_media_uri(BARE, session=transport))
    assert got["media_type"] == "image"
    assert got["mime_type"] == "image/png"


def test_the_classification_costs_a_head_not_a_get():
    """A GET here would download the body to learn one header — the
    point is to classify without paying for the bytes."""
    fetcher, transport = wired({BARE: Reply(headers={"Content-Type": "image/png"})})
    run(fetcher.check_media_uri(BARE, session=transport))
    assert transport.heads == [BARE]
    assert transport.requests == []


def test_a_declared_length_is_reported_as_a_number():
    fetcher, transport = wired({BARE: Reply(headers={"Content-Type": "image/png",
                                                     "Content-Length": "4096"})})
    assert run(fetcher.check_media_uri(BARE, session=transport))["content_length"] == 4096


def test_a_missing_length_is_reported_as_nothing():
    fetcher, transport = wired({BARE: Reply(headers={"Content-Type": "image/png"})})
    assert run(fetcher.check_media_uri(BARE, session=transport))["content_length"] is None


def test_the_classification_reports_the_address_after_redirects():
    final = "https://cdn.example.com/asset/9182.png"
    fetcher, transport = wired({BARE: Reply(headers={"Content-Type": "image/png"},
                                            final_uri=final)})
    assert _loc(run(fetcher.check_media_uri(BARE, session=transport))) == final


def test_a_non_media_address_classifies_as_nothing():
    fetcher, transport = wired({BARE: Reply(headers={"Content-Type": "text/html"})})
    assert run(fetcher.check_media_uri(BARE, session=transport)) is None


def test_a_non_200_classifies_as_nothing():
    fetcher, transport = wired({BARE: Reply(status=404)})
    assert run(fetcher.check_media_uri(BARE, session=transport)) is None


def test_a_failing_head_classifies_as_nothing():
    """A server refusing HEAD is common; it must not raise into the
    caller's crawl."""
    fetcher, transport = wired({BARE: RuntimeError("HEAD not allowed")})
    assert run(fetcher.check_media_uri(BARE, session=transport)) is None


def test_the_classification_closes_a_session_it_built(monkeypatch):
    transport = Transport({BARE: Reply(headers={"Content-Type": "image/png"})})
    monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda *a, **k: transport)
    fetcher = AsyncWebFetcher(fast_mode=True)
    assert run(fetcher.check_media_uri(BARE)) is not None
    assert transport.closed
