"""Crawl orchestration, with the network replaced at one seam.

``fetch_with_depth`` is the largest function in the fetcher and it sat
entirely unmeasured — it is the BFS that decides which pages a crawl
visits, in what order, and when it stops. Everything a crawl budget
buys is decided here.

It touches the network through exactly ONE method, ``fetch_webpage``.
Stubbing that and nothing else leaves the whole orchestration real:
depth limiting, the page ceiling, visited-set dedup, fragment
collapsing, domain preference, keyword scoring, the resume path, and
every callback. A test that stubbed the scoring or the queue instead
would be testing its own fake.

This file is SHARED VERBATIM across arborist,
agents.ai.unturf.com, crawler.unturf.com and neopig, like the fetcher it
tests. The import path is the one thing that cannot be shared, so it
lives in each repo's conftest.py, bound to the name
``shared_web_fetcher``. `make ship-delta` (in crawler.unturf.com) fails
on any drift.
"""

from __future__ import annotations

import asyncio

import pytest


pytest.importorskip("shared_web_fetcher")

from shared_web_fetcher import (  # noqa: E402
    AsyncWebFetcher,
    CrawlMode,
)

HOME = "https://example.com/"


def _page(*links: str, text: str = "Some page text about merkle proofs.") -> str:
    body = "".join(f'<a href="{href}">link {i}</a>' for i, href in enumerate(links))
    return f"<html><head><title>A Page</title></head><body><p>{text}</p>{body}</body></html>"


def _loc(item):
    """The located address of a media item, page or feed entry."""
    if isinstance(item, dict):
        return item.get("uri")
    return getattr(item, "uri", None)


class Site:
    """A fake web: address -> html. Records what was fetched, in order."""

    def __init__(self, pages: dict, fail: set = None):
        self.pages = pages
        self.fail = fail or set()
        self.fetched: list = []

    def install(self, fetcher):
        async def _fetch_webpage(uri, extract_links=False,
                                 extract_anchor_text=False,
                                 cache_check_callback=None):
            self.fetched.append(uri)
            if uri in self.fail or uri not in self.pages:
                return None, []
            html = self.pages[uri]
            links = []
            if extract_links:
                from bs4 import BeautifulSoup
                from urllib.parse import urljoin
                soup = BeautifulSoup(html, "html.parser")
                for a in soup.find_all("a", href=True):
                    absolute = urljoin(uri, a["href"])
                    if extract_anchor_text:
                        links.append({"uri": absolute, "url": absolute,
                                      "anchor_text": a.get_text(strip=True)})
                    else:
                        links.append(absolute)
            return html, links

        fetcher.fetch_webpage = _fetch_webpage
        return self


def crawl(site: Site, start=HOME, **kw):
    """Run one crawl against a fake site and return its pages."""
    fetcher = AsyncWebFetcher()
    site.install(fetcher)
    kw["start_uri"] = start
    kw.setdefault("depth", 2)
    kw.setdefault("max_pages", 10)
    return asyncio.run(fetcher.fetch_with_depth(**kw))


# --------------------------------------------------------------------
# The start page
# --------------------------------------------------------------------


def test_a_single_page_site_yields_that_page():
    site = Site({HOME: _page()})
    assert len(crawl(site)) == 1


def test_the_start_page_is_fetched_first():
    """Phase 1 is unconditional: whatever else the scoring decides, the
    page the caller asked about is fetched."""
    site = Site({HOME: _page("/a"), HOME + "a": _page()})
    crawl(site)
    assert site.fetched[0] == HOME


def test_an_unreachable_start_page_yields_nothing():
    """No seed, no crawl — and no exception either, since a dead start
    address is an ordinary outcome for a crawler."""
    site = Site({}, fail={HOME})
    assert crawl(site) == []


def test_a_result_carries_its_address_and_title():
    site = Site({HOME: _page()})
    page = crawl(site)[0]
    assert _loc(page) == HOME
    assert page.get("title")


def test_a_result_carries_extracted_text():
    site = Site({HOME: _page(text="A distinctive sentence about merkle.")})
    assert "distinctive" in crawl(site)[0].get("text", "")


# --------------------------------------------------------------------
# Following links
# --------------------------------------------------------------------


def test_linked_pages_are_followed():
    site = Site({HOME: _page("/a", "/b"),
                 HOME + "a": _page(), HOME + "b": _page()})
    assert len(crawl(site)) == 3


def test_depth_zero_follows_nothing():
    """The ceiling the caller paid for. depth=0 is "this page only"."""
    site = Site({HOME: _page("/a"), HOME + "a": _page()})
    assert len(crawl(site, depth=0)) == 1


def test_depth_one_follows_children_but_not_grandchildren():
    site = Site({HOME: _page("/a"), HOME + "a": _page("/b"),
                 HOME + "b": _page()})
    got = {_loc(p) for p in crawl(site, depth=1)}
    assert HOME + "a" in got
    assert HOME + "b" not in got


def test_the_page_ceiling_is_respected():
    """max_pages is a cost bound. Exceeding it spends budget the caller
    did not agree to."""
    pages = {HOME: _page(*[f"/p{i}" for i in range(20)])}
    pages.update({f"{HOME}p{i}": _page() for i in range(20)})
    assert len(crawl(Site(pages), max_pages=5)) <= 5


def test_a_page_is_never_fetched_twice():
    """Two pages linking to the same third is the normal shape of a
    site; fetching it twice doubles the cost for nothing."""
    site = Site({HOME: _page("/a", "/b"),
                 HOME + "a": _page("/shared"),
                 HOME + "b": _page("/shared"),
                 HOME + "shared": _page()})
    crawl(site, depth=2)
    assert site.fetched.count(HOME + "shared") <= 1


def test_a_self_link_does_not_loop():
    site = Site({HOME: _page("/", "/a"), HOME + "a": _page()})
    crawl(site)
    assert site.fetched.count(HOME) <= 1


def test_a_link_cycle_terminates():
    """a -> b -> a. Without a visited set this never returns."""
    site = Site({HOME: _page("/a"),
                 HOME + "a": _page("/b"),
                 HOME + "b": _page("/a")})
    assert len(crawl(site, depth=3)) <= 3


def test_links_differing_only_by_fragment_are_one_page():
    """#section is a scroll position, not a document."""
    site = Site({HOME: _page("/a#one", "/a#two"), HOME + "a": _page()})
    crawl(site)
    assert site.fetched.count(HOME + "a") <= 1


def test_a_failing_child_does_not_end_the_crawl():
    """One dead link is not a reason to abandon the pages that work."""
    site = Site({HOME: _page("/dead", "/alive"), HOME + "alive": _page()},
                fail={HOME + "dead"})
    got = {_loc(p) for p in crawl(site)}
    assert HOME + "alive" in got


# --------------------------------------------------------------------
# Resume
# --------------------------------------------------------------------


def test_pre_visited_addresses_are_not_refetched():
    """The resume path. A crawl that restarts must not re-pay for
    everything it already has."""
    site = Site({HOME: _page("/a", "/b"),
                 HOME + "a": _page(), HOME + "b": _page()})
    crawl(site, initial_visited={HOME + "a"})
    assert HOME + "a" not in site.fetched


def test_an_empty_visited_set_behaves_like_a_fresh_crawl():
    site = Site({HOME: _page("/a"), HOME + "a": _page()})
    assert len(crawl(site, initial_visited=set())) == 2


# --------------------------------------------------------------------
# Keyword steering
# --------------------------------------------------------------------


def test_keywords_do_not_break_the_crawl():
    """Supplying keywords switches on anchor-text extraction and
    scoring — a different code path through the same walk."""
    site = Site({HOME: _page("/merkle-proofs", "/gardening"),
                 HOME + "merkle-proofs": _page(),
                 HOME + "gardening": _page()})
    assert len(crawl(site, query_keywords=["merkle"])) >= 2


def test_keyword_variations_are_accepted():
    site = Site({HOME: _page("/a"), HOME + "a": _page()})
    assert crawl(site, query_keywords=["merkle"],
                 keyword_variations=["hash", "tree"])


def test_a_relevant_link_is_preferred_when_the_budget_is_tight():
    """With room for one child, the keyword-matching one should be the
    one that gets it — that is what the scoring is for."""
    site = Site({HOME: _page("/gardening", "/merkle-proofs"),
                 HOME + "gardening": _page(),
                 HOME + "merkle-proofs": _page()})
    got = {_loc(p) for p in crawl(site, max_pages=2,
                                  query_keywords=["merkle"])}
    assert HOME + "merkle-proofs" in got


# --------------------------------------------------------------------
# Callbacks
# --------------------------------------------------------------------


def test_the_page_callback_receives_each_page():
    seen = []

    async def _page_cb(uri, html):
        seen.append(uri)

    site = Site({HOME: _page("/a"), HOME + "a": _page()})
    crawl(site, page_callback=_page_cb)
    assert HOME in seen


def test_a_progress_callback_is_invoked():
    """It is awaited, so the contract is an async callable — a sync one
    raises inside the crawl rather than being called."""
    calls = []

    async def _progress(message):
        calls.append(message)

    site = Site({HOME: _page("/a"), HOME + "a": _page()})
    crawl(site, progress_callback=_progress)
    assert calls
    assert any("Fetched page" in c for c in calls)


def test_a_raising_page_callback_does_not_lose_the_crawl():
    """A consumer's bug should cost that page's side effect, not the
    pages already fetched."""
    async def _boom(uri, html):
        raise RuntimeError("consumer exploded")

    site = Site({HOME: _page("/a"), HOME + "a": _page()})
    try:
        pages = crawl(site, page_callback=_boom)
    except RuntimeError:
        pytest.fail("a callback failure ended the crawl")
    assert isinstance(pages, list)


# --------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------


def test_text_mode_returns_pages():
    site = Site({HOME: _page()})
    assert crawl(site, mode=CrawlMode.TEXT)


def test_a_media_mode_crawl_still_walks_pages():
    """Media collection rides on the same walk; switching mode must not
    stop the crawl finding pages."""
    site = Site({HOME: _page("/a"), HOME + "a": _page()})
    assert crawl(site, mode=CrawlMode.IMAGES, depth=1) is not None


# --------------------------------------------------------------------
# Unlimited budgets
# --------------------------------------------------------------------


def _chain(n: int) -> dict:
    """A -> B -> C -> ... n links deep, one link per page."""
    pages = {HOME: _page("/p1")}
    for i in range(1, n):
        pages[f"{HOME}p{i}"] = _page(f"/p{i + 1}")
    pages[f"{HOME}p{n}"] = _page()
    return pages


def test_depth_minus_one_means_unlimited():
    """-1 is the full-domain crawl. A literal depth of -1 would stop
    before the first link."""
    site = Site(_chain(4))
    assert len(crawl(site, depth=-1, max_pages=50)) == 5


def test_max_pages_minus_one_means_unlimited():
    site = Site(_chain(4))
    assert len(crawl(site, depth=10, max_pages=-1)) == 5


# --------------------------------------------------------------------
# Feeds are not pages
# --------------------------------------------------------------------


FEED_XML = ('<?xml version="1.0"?><rss version="2.0"><channel>'
            '<item><link>https://example.com/one</link></item>'
            '</channel></rss>')


@pytest.mark.parametrize("uri", [
    "https://example.com/feed.xml",
    "https://example.com/index.rss",
    "https://example.com/all.atom",
    "https://example.com/blog/feed",
    "https://example.com/rss/",
])
def test_a_feed_address_is_not_crawled_as_a_page(uri):
    """Text extraction over a feed produces noise, and the crawl would
    then follow whatever the noise looked like."""
    site = Site({uri: _page()})
    assert crawl(site, start=uri) == []


def test_feed_content_served_from_a_page_address_is_refused():
    """Address alone is not enough — plenty of feeds live at ordinary
    addresses, so the body is checked too."""
    site = Site({HOME: FEED_XML})
    assert crawl(site) == []


def test_a_child_link_serving_a_feed_is_skipped():
    site = Site({HOME: _page("/feed.xml", "/real"),
                 HOME + "feed.xml": FEED_XML,
                 HOME + "real": _page()})
    got = {_loc(p) for p in crawl(site)}
    assert HOME + "real" in got
    assert HOME + "feed.xml" not in got


def test_a_child_serving_feed_content_from_a_page_address_is_skipped():
    site = Site({HOME: _page("/looks-normal", "/real"),
                 HOME + "looks-normal": FEED_XML,
                 HOME + "real": _page()})
    got = {_loc(p) for p in crawl(site)}
    assert HOME + "looks-normal" not in got


# --------------------------------------------------------------------
# Hydra: priority addresses from feeds and sitemaps
# --------------------------------------------------------------------


def test_hydra_addresses_are_crawled():
    """Addresses that came from a feed are known-good content; they
    enter the registry pre-scored so they go first. (Named away from
    the word "feed": the feed guard matches any address ENDING in it,
    so a page called /from-feed is skipped as a feed.)"""
    site = Site({HOME: _page(), HOME + "listed": _page()})
    got = {_loc(p) for p in crawl(site, hydra_uris=[HOME + "listed"])}
    assert HOME + "listed" in got


def test_hydra_addresses_outrank_ordinary_links():
    site = Site({HOME: _page("/ordinary"),
                 HOME + "ordinary": _page(),
                 HOME + "listed": _page()})
    got = crawl(site, hydra_uris=[HOME + "listed"], max_pages=2)
    assert HOME + "listed" in {_loc(p) for p in got}


def test_an_already_visited_hydra_address_is_not_refetched():
    site = Site({HOME: _page(), HOME + "seen": _page()})
    crawl(site, hydra_uris=[HOME + "seen"],
          initial_visited={HOME + "seen"})
    assert HOME + "seen" not in site.fetched


# --------------------------------------------------------------------
# Cross-domain links
# --------------------------------------------------------------------


def _linked(href: str, anchor: str) -> str:
    return (f'<html><head><title>T</title></head><body><p>text</p>'
            f'<a href="{href}">{anchor}</a></body></html>')


def test_a_weak_cross_domain_link_is_not_followed():
    """Leaving the domain the crawl was pointed at needs a strong
    reason; a bare link is not one."""
    other = "https://other.example.org/page"
    site = Site({HOME: _linked(other, "some link"), other: _page()})
    got = {_loc(p) for p in crawl(site, query_keywords=["merkle"])}
    assert other not in got


def test_a_strongly_matching_cross_domain_link_is_followed():
    """Two keywords matched in both the address and the anchor reach
    the threshold, which is what the threshold is for."""
    other = "https://other.example.org/merkle/proof"
    site = Site({HOME: _linked(other, "merkle proof explained"),
                 other: _page()})
    got = {_loc(p) for p in crawl(site, query_keywords=["merkle", "proof"])}
    assert other in got


def test_a_same_domain_link_with_no_keyword_match_is_skipped():
    """Once keywords are in play a same-domain link still has to earn
    its fetch."""
    site = Site({HOME: _linked("/gardening", "gardening"),
                 HOME + "gardening": _page()})
    got = {_loc(p) for p in crawl(site, query_keywords=["merkle"])}
    assert HOME + "gardening" not in got


def _links(*pairs) -> str:
    body = "".join(f'<a href="{href}">{anchor}</a>' for href, anchor in pairs)
    return f'<html><head><title>T</title></head><body><p>t</p>{body}</body></html>'


def test_a_link_seen_from_several_pages_outranks_one_seen_once():
    """Repeat sightings accumulate rather than replace, so with room
    for one more page the link the whole site points at is the one that
    gets it."""
    site = Site({
        HOME: _links(("/merkle/a", "merkle a"), ("/merkle/b", "merkle b")),
        HOME + "merkle/a": _links(("/merkle/popular", "merkle popular"),
                                  ("/merkle/lonely", "merkle lonely")),
        HOME + "merkle/b": _links(("/merkle/popular", "merkle popular")),
        HOME + "merkle/popular": _page(),
        HOME + "merkle/lonely": _page(),
    })
    got = {_loc(p) for p in crawl(site, depth=2, max_pages=4,
                                  query_keywords=["merkle"])}
    assert HOME + "merkle/popular" in got
    assert HOME + "merkle/lonely" not in got


def test_sightings_alone_do_not_make_an_unmatched_link_eligible():
    """The boost changes ORDER, not eligibility: a same-domain link is
    filtered on its raw keyword score, so a link with no match is
    skipped however many pages point at it. (Cross-domain links are
    filtered on the boosted score instead — the asymmetry is the
    behaviour, pinned here so a change to it is deliberate.)"""
    site = Site({
        HOME: _links(("/merkle/a", "merkle a"), ("/merkle/b", "merkle b")),
        HOME + "merkle/a": _links(("/gardening", "gardening")),
        HOME + "merkle/b": _links(("/gardening", "gardening")),
        HOME + "gardening": _page(),
    })
    got = {_loc(p) for p in crawl(site, depth=3, query_keywords=["merkle"])}
    assert HOME + "gardening" not in got


# --------------------------------------------------------------------
# Callbacks and reporting
# --------------------------------------------------------------------


def test_the_media_callback_receives_discovered_media():
    seen = []

    async def _media(item):
        seen.append(item)

    html = ('<html><head><title>T</title></head>'
            '<body><img src="/i/a.png"></body></html>')
    site = Site({HOME: html})
    crawl(site, mode=CrawlMode.IMAGES, media_callback=_media)
    assert [_loc(i) for i in seen] == ["https://example.com/i/a.png"]


def test_the_media_callback_runs_for_child_pages_too():
    seen = []

    async def _media(item):
        seen.append(item)

    child = ('<html><head><title>T</title></head>'
             '<body><img src="/i/child.png"></body></html>')
    site = Site({HOME: _page("/a"), HOME + "a": child})
    crawl(site, mode=CrawlMode.IMAGES, media_callback=_media)
    assert "https://example.com/i/child.png" in [_loc(i) for i in seen]


def test_a_raising_media_callback_does_not_lose_the_crawl():
    async def _boom(item):
        raise RuntimeError("consumer exploded")

    html = ('<html><head><title>T</title></head>'
            '<body><img src="/i/a.png"></body></html>')
    site = Site({HOME: html})
    assert crawl(site, mode=CrawlMode.IMAGES, media_callback=_boom)


def test_the_discovered_total_is_reported():
    """The consumer draws a progress bar with this, so it has to be
    told when the denominator grows."""
    totals = []
    site = Site({HOME: _page("/a", "/b"),
                 HOME + "a": _page("/c"), HOME + "b": _page(),
                 HOME + "c": _page()})
    crawl(site, uris_total_callback=totals.append)
    assert totals
    assert totals[-1] >= totals[0]


# --------------------------------------------------------------------
# Failure inside the parallel wave
# --------------------------------------------------------------------


def test_a_raising_fetch_does_not_end_the_crawl():
    """fetch_webpage returns (None, []) on ordinary failure, but a
    consumer-supplied cache callback can raise from inside it; the
    parallel wave must absorb that."""
    class Exploding(Site):
        def install(self, fetcher):
            super().install(fetcher)
            inner = fetcher.fetch_webpage

            async def _fetch(uri, **kw):
                if uri.endswith("boom"):
                    raise RuntimeError("fetch exploded")
                return await inner(uri, **kw)

            fetcher.fetch_webpage = _fetch
            return self

    site = Exploding({HOME: _page("/boom", "/fine"),
                      HOME + "fine": _page()})
    got = {_loc(p) for p in crawl(site)}
    assert HOME + "fine" in got
    assert HOME + "boom" not in got


def test_the_page_ceiling_stops_a_wave_mid_flight():
    """The ceiling is checked as results arrive, not only when the wave
    is queued — a wide fan-out must not overshoot."""
    pages = {HOME: _page(*[f"/p{i}" for i in range(12)])}
    pages.update({f"{HOME}p{i}": _page() for i in range(12)})
    assert len(crawl(Site(pages), max_pages=3)) <= 3


def test_a_depth_that_yields_nothing_ends_the_crawl():
    """Every link at this depth failed or was skipped, so a deeper pass
    has nothing to work from."""
    site = Site({HOME: _page("/dead1", "/dead2")},
                fail={HOME + "dead1", HOME + "dead2"})
    assert len(crawl(site, depth=5)) == 1
