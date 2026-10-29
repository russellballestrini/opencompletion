"""Hermetic tests for the fetcher's pure surface.

Everything here is string-in / value-out: feed parsing, feed discovery,
robots.txt explanation, link normalisation, link scoring, and the
token-budget knapsack. No sockets, no aiohttp, no event loop.

That surface was almost entirely unmeasured — the feed parsers, feed
discovery and the robots analyser all sat at 0% while being pure
functions with no reason to be. A crawler's feed and robots handling
decides what it will and will not fetch, so a silent failure there is
not a crash, it is a crawl that quietly does less than it reports.

This file is SHARED VERBATIM across arborist,
agents.ai.unturf.com, crawler.unturf.com and neopig, like the fetcher it
tests. The import path is the one thing that cannot be shared, so it
lives in each repo's conftest.py, bound to the name
``shared_web_fetcher``. `make ship-delta` (in crawler.unturf.com) fails
on any drift.
"""

from __future__ import annotations

import pytest


pytest.importorskip("shared_web_fetcher")

import shared_web_fetcher as mod  # noqa: E402
from shared_web_fetcher import (  # noqa: E402
    AsyncWebFetcher,
    analyze_robots_txt_blocking,
    discover_feeds,
    normalize_link,
    parse_atom,
    parse_feed,
    parse_rss,
    strip_uri_fragment,
)

BASE = "https://example.com/"


def _loc(item):
    """The located address of a media item, page or feed entry."""
    if isinstance(item, dict):
        return item.get("uri")
    return getattr(item, "uri", None)


# --------------------------------------------------------------------
# parse_rss
# --------------------------------------------------------------------


RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>One</title><link>https://example.com/1</link>
      <pubDate>Mon, 01 Jan 2026 00:00:00 GMT</pubDate></item>
<item><title>Two</title><link>/two</link></item>
</channel></rss>"""


def test_rss_items_are_extracted():
    items = parse_rss(RSS, BASE)
    assert [_loc(i) for i in items] == ["https://example.com/1",
                                        "https://example.com/two"]


def test_rss_titles_are_carried():
    assert [i.title for i in parse_rss(RSS, BASE)] == ["One", "Two"]


def test_an_rss_publication_date_is_carried():
    assert parse_rss(RSS, BASE)[0].published.startswith("Mon, 01 Jan 2026")


def test_an_rss_item_without_a_date_is_still_returned():
    """A feed that omits pubDate is common and not a reason to drop the
    entry."""
    assert parse_rss(RSS, BASE)[1].published is None


def test_a_relative_rss_link_resolves_against_the_base():
    assert _loc(parse_rss(RSS, BASE)[1]) == "https://example.com/two"


def test_an_rss_item_with_no_link_is_skipped():
    """Nothing to fetch, so nothing to queue."""
    xml = ("<rss><channel><item><title>No link</title></item>"
           "<item><link>/ok</link></item></channel></rss>")
    assert len(parse_rss(xml, BASE)) == 1


def test_a_malformed_rss_yields_nothing_rather_than_raising():
    """One broken feed must not end a crawl."""
    assert parse_rss("<not xml", BASE) == []


def test_an_empty_rss_channel_yields_nothing():
    assert parse_rss("<rss><channel></channel></rss>", BASE) == []


# --------------------------------------------------------------------
# parse_atom
# --------------------------------------------------------------------


ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Alpha</title><link href="https://example.com/a"/>
       <published>2026-01-01T00:00:00Z</published>
       <updated>2026-02-01T00:00:00Z</updated></entry>
<entry><title>Beta</title><link href="/b"/></entry>
</feed>"""


def test_atom_entries_are_extracted():
    items = parse_atom(ATOM, BASE)
    assert [_loc(i) for i in items] == ["https://example.com/a",
                                        "https://example.com/b"]


def test_an_atom_link_is_read_from_its_href():
    """Atom puts the address in an attribute, not in element text —
    the opposite of RSS."""
    assert _loc(parse_atom(ATOM, BASE)[0]) == "https://example.com/a"


def test_atom_published_and_updated_are_distinct_fields():
    e = parse_atom(ATOM, BASE)[0]
    assert e.published.startswith("2026-01-01")
    assert e.updated.startswith("2026-02-01")


def test_an_atom_link_falls_back_to_element_text():
    """Some feeds write <link>https://…</link> instead of an href."""
    xml = ("<feed xmlns='http://www.w3.org/2005/Atom'><entry>"
           "<link>https://example.com/text-form</link></entry></feed>")
    assert _loc(parse_atom(xml, BASE)[0]) == "https://example.com/text-form"


def test_an_atom_entry_with_no_link_is_skipped():
    xml = ("<feed xmlns='http://www.w3.org/2005/Atom'>"
           "<entry><title>No link</title></entry></feed>")
    assert parse_atom(xml, BASE) == []


def test_a_malformed_atom_yields_nothing_rather_than_raising():
    assert parse_atom("<not xml", BASE) == []


# --------------------------------------------------------------------
# parse_feed — the dispatcher
# --------------------------------------------------------------------


def test_parse_feed_reads_an_rss_document():
    items, subs = parse_feed(RSS, BASE)
    assert len(items) == 2 and subs == []


def test_parse_feed_reads_an_atom_document():
    items, subs = parse_feed(ATOM, BASE)
    assert len(items) == 2 and subs == []


def test_parse_feed_reads_a_sitemap():
    """The one branch that can return sub-feeds — a sitemap index
    points at further sitemaps."""
    xml = ("<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
           "<sitemap><loc>https://example.com/s1.xml</loc></sitemap>"
           "</sitemapindex>")
    items, subs = parse_feed(xml, BASE)
    assert subs == ["https://example.com/s1.xml"]


def test_parse_feed_on_unrecognised_content_yields_nothing():
    """Always a 2-tuple, on every branch. The caller unpacks it
    directly, so a bare list on one path would raise there rather than
    here."""
    items, subs = parse_feed("<html><body>not a feed</body></html>", BASE)
    assert items == [] and subs == []


def test_parse_feed_detects_the_type_from_the_content_not_the_uri():
    """A feed served from a path with no useful extension still has to
    parse."""
    items, _ = parse_feed(RSS, "https://example.com/opaque-endpoint")
    assert len(items) == 2


# --------------------------------------------------------------------
# discover_feeds
# --------------------------------------------------------------------


def test_an_rss_alternate_link_is_discovered():
    html = ('<link rel="alternate" type="application/rss+xml" '
            'href="/feed.xml">')
    assert "https://example.com/feed.xml" in discover_feeds(html, BASE)


def test_an_atom_alternate_link_is_discovered():
    html = ('<link rel="alternate" type="application/atom+xml" '
            'href="/atom.xml">')
    assert "https://example.com/atom.xml" in discover_feeds(html, BASE)


def test_an_alternate_link_of_another_type_is_not_a_feed():
    """rel=alternate also carries translations and print stylesheets;
    queueing those as feeds wastes fetches on documents that parse to
    nothing."""
    html = ('<link rel="alternate" type="text/html" hreflang="fr" '
            'href="/fr/">')
    assert "https://example.com/fr/" not in discover_feeds(html, BASE)


def test_a_declared_sitemap_link_is_discovered():
    html = '<link rel="sitemap" href="/custom-sitemap.xml">'
    assert "https://example.com/custom-sitemap.xml" in discover_feeds(html, BASE)


def test_the_common_sitemap_locations_are_probed():
    """A site that declares nothing still usually has /sitemap.xml, and
    guessing it is free relative to crawling blind."""
    feeds = discover_feeds("<html></html>", BASE)
    assert any(f.endswith("/sitemap.xml") for f in feeds)


def test_discovery_survives_html_with_no_head():
    assert isinstance(discover_feeds("plain text", BASE), list)


def test_discovered_feeds_are_absolute():
    feeds = discover_feeds('<link rel="alternate" type="application/rss+xml" '
                           'href="feed.xml">', BASE)
    assert all(f.startswith("http") for f in feeds)


# --------------------------------------------------------------------
# analyze_robots_txt_blocking
# --------------------------------------------------------------------


def test_a_blocking_rule_is_named():
    """The point of this function: say WHICH rule stopped the fetch, so
    an operator can tell a deliberate exclusion from a mistake."""
    robots = "User-agent: *\nDisallow: /private/\n"
    out = analyze_robots_txt_blocking(robots, "https://example.com/private/x")
    assert "/private/" in out


def test_a_wildcard_agent_rule_applies_to_us():
    robots = "User-agent: *\nDisallow: /no/\n"
    out = analyze_robots_txt_blocking(robots, "https://example.com/no/page")
    assert out


def test_a_rule_for_another_agent_is_reported_differently():
    """A Disallow aimed at some other crawler did not block us, and
    saying it did would send someone chasing the wrong line."""
    robots = "User-agent: BadBot\nDisallow: /private/\n"
    ours = analyze_robots_txt_blocking(
        "User-agent: *\nDisallow: /private/\n",
        "https://example.com/private/x")
    theirs = analyze_robots_txt_blocking(
        robots, "https://example.com/private/x")
    assert ours != theirs


def test_comments_and_blank_lines_are_ignored():
    robots = "# a comment\n\nUser-agent: *\nDisallow: /x/\n\n"
    assert analyze_robots_txt_blocking(robots, "https://example.com/x/y")


def test_an_empty_robots_txt_still_returns_an_explanation():
    """Never None — the caller renders this straight into an operator
    message."""
    assert isinstance(
        analyze_robots_txt_blocking("", "https://example.com/x"), str)


def test_a_uri_with_no_path_is_treated_as_root():
    assert isinstance(
        analyze_robots_txt_blocking("User-agent: *\nDisallow: /\n",
                                    "https://example.com"), str)


# --------------------------------------------------------------------
# strip_uri_fragment / normalize_link
# --------------------------------------------------------------------


def test_a_fragment_is_stripped():
    """Two links differing only by #anchor are the same fetch; keeping
    the fragment would crawl the same page once per heading."""
    assert strip_uri_fragment("https://example.com/p#section") == (
        "https://example.com/p")


def test_a_query_survives_fragment_stripping():
    assert strip_uri_fragment("https://example.com/p?a=1#x") == (
        "https://example.com/p?a=1")


def test_a_uri_without_a_fragment_is_unchanged():
    assert strip_uri_fragment("https://example.com/p") == (
        "https://example.com/p")


def test_an_empty_uri_is_returned_as_is():
    assert strip_uri_fragment("") == ""


def test_a_dict_link_yields_its_address_and_anchor():
    uri, anchor = normalize_link({"uri": "https://example.com/p#f",
                                  "url": "https://example.com/p#f",
                                  "anchor_text": "Click me"})
    assert uri == "https://example.com/p"
    assert anchor == "Click me"


def test_a_bare_string_link_yields_an_empty_anchor():
    uri, anchor = normalize_link("https://example.com/p#f")
    assert uri == "https://example.com/p"
    assert anchor == ""


def test_a_dict_link_without_an_anchor_yields_empty():
    uri, anchor = normalize_link({"uri": "https://example.com/p",
                                  "url": "https://example.com/p"})
    assert anchor == ""


# --------------------------------------------------------------------
# _score_link
# --------------------------------------------------------------------


@pytest.fixture()
def fetcher():
    return AsyncWebFetcher()


def test_no_keywords_scores_zero(fetcher):
    """Nothing to be relevant TO, so every link ranks alike and the
    caller falls back to document order."""
    assert fetcher._score_link("https://example.com/x", "anything") == 0.0


def test_a_keyword_in_the_uri_scores(fetcher):
    assert fetcher._score_link(
        "https://example.com/merkle-proofs", "", ["merkle"]) > 0


def test_a_keyword_in_the_anchor_scores(fetcher):
    assert fetcher._score_link(
        "https://example.com/x", "about merkle trees", ["merkle"]) > 0


def test_a_uri_match_outweighs_an_anchor_match(fetcher):
    """The address is the site's own classification of the page; anchor
    text is whatever one author happened to write."""
    in_uri = fetcher._score_link("https://example.com/merkle", "", ["merkle"])
    in_anchor = fetcher._score_link("https://example.com/x", "merkle",
                                    ["merkle"])
    assert in_uri > in_anchor


def test_a_primary_keyword_outweighs_a_variation(fetcher):
    """Variations are synonyms the caller supplied, not what was asked
    for."""
    primary = fetcher._score_link("https://example.com/merkle", "",
                                  ["merkle"], [])
    variation = fetcher._score_link("https://example.com/merkle", "",
                                    [], ["merkle"])
    assert primary > variation


def test_matching_is_case_insensitive(fetcher):
    assert fetcher._score_link("https://example.com/MERKLE", "",
                               ["merkle"]) > 0


def test_an_unrelated_link_scores_zero(fetcher):
    assert fetcher._score_link("https://example.com/gardening", "roses",
                               ["merkle"]) == 0.0


def test_more_matching_keywords_score_higher(fetcher):
    one = fetcher._score_link("https://example.com/merkle", "", ["merkle"])
    two = fetcher._score_link("https://example.com/merkle-proof", "",
                              ["merkle", "proof"])
    assert two > one


# --------------------------------------------------------------------
# select_pages_by_token_budget
# --------------------------------------------------------------------


def _page(score, tokens, uri="https://example.com/p"):
    return {"score": score, "tokens": tokens, "uri": uri, "url": uri,
            "title": "T", "text": "x"}


def test_no_pages_select_nothing(fetcher):
    assert fetcher.select_pages_by_token_budget([], 10_000) == []


def test_pages_are_selected_highest_score_first(fetcher):
    pages = [_page(1.0, 100, "a"), _page(9.0, 100, "b")]
    out = fetcher.select_pages_by_token_budget(pages, 10_000, reserve_tokens=0)
    assert _loc(out[0]) == "b"


def test_selection_stops_at_the_budget(fetcher):
    pages = [_page(9.0, 600), _page(8.0, 600), _page(7.0, 600)]
    out = fetcher.select_pages_by_token_budget(pages, 1500, reserve_tokens=0)
    assert sum(p["tokens"] for p in out) <= 1500


def test_the_reserve_is_taken_off_the_budget(fetcher):
    """Tokens held back for the system prompt and the answer. Spending
    them on context is how a request overruns its window."""
    pages = [_page(9.0, 900)]
    assert fetcher.select_pages_by_token_budget(
        pages, 1000, reserve_tokens=500) == []


def test_a_budget_smaller_than_the_reserve_selects_nothing(fetcher):
    assert fetcher.select_pages_by_token_budget(
        [_page(9.0, 10)], 100, reserve_tokens=1000) == []


def test_a_page_that_does_not_fit_does_not_block_a_smaller_one(fetcher):
    """Greedy by score, but a giant page must not starve the rest of the
    budget."""
    pages = [_page(9.0, 5000, "big"), _page(8.0, 100, "small")]
    out = fetcher.select_pages_by_token_budget(pages, 1000, reserve_tokens=0)
    assert [_loc(p) for p in out] == ["small"]


# --------------------------------------------------------------------
# PDF text extraction
#
# pypdf does the parsing; what is ours is the page loop, the join, and
# refusing to return an empty string as if it were text. A fake reader
# isolates exactly that.
# --------------------------------------------------------------------


class _FakePage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        if isinstance(self._text, BaseException):
            raise self._text
        return self._text


class _FakeReader:
    def __init__(self, *pages):
        self.pages = [_FakePage(p) for p in pages]


def _reader(*pages):
    return lambda stream: _FakeReader(*pages)


def test_pdf_text_is_extracted(monkeypatch):
    monkeypatch.setattr(mod, "PDF_SUPPORT", True)
    monkeypatch.setattr(mod, "PdfReader", _reader("Some prose."))
    assert mod.extract_text_from_pdf(b"%PDF-1.4") == "Some prose."


def test_pages_are_joined_with_a_blank_line(monkeypatch):
    """The break has to survive: chunkers downstream split on it, and a
    page boundary is a real boundary in the source."""
    monkeypatch.setattr(mod, "PDF_SUPPORT", True)
    monkeypatch.setattr(mod, "PdfReader", _reader("One.", "Two."))
    assert mod.extract_text_from_pdf(b"%PDF") == "One.\n\nTwo."


def test_an_empty_page_is_skipped(monkeypatch):
    """A scanned page with no text layer yields '' — joining it would
    fabricate a blank page break that is not in the document."""
    monkeypatch.setattr(mod, "PDF_SUPPORT", True)
    monkeypatch.setattr(mod, "PdfReader", _reader("One.", "", "Three."))
    assert mod.extract_text_from_pdf(b"%PDF") == "One.\n\nThree."


def test_a_pdf_with_no_text_at_all_yields_nothing(monkeypatch):
    """None, not "" — the caller branches on it to decide the PDF was
    unextractable rather than empty."""
    monkeypatch.setattr(mod, "PDF_SUPPORT", True)
    monkeypatch.setattr(mod, "PdfReader", _reader("", ""))
    assert mod.extract_text_from_pdf(b"%PDF") is None


def test_a_pdf_with_no_pages_yields_nothing(monkeypatch):
    monkeypatch.setattr(mod, "PDF_SUPPORT", True)
    monkeypatch.setattr(mod, "PdfReader", _reader())
    assert mod.extract_text_from_pdf(b"%PDF") is None


def test_unparseable_bytes_yield_nothing_rather_than_raising(monkeypatch):
    """One corrupt PDF in a crawl must cost that page, not the run."""
    def _boom(stream):
        raise ValueError("not a PDF")

    monkeypatch.setattr(mod, "PDF_SUPPORT", True)
    monkeypatch.setattr(mod, "PdfReader", _boom)
    assert mod.extract_text_from_pdf(b"garbage") is None


def test_a_page_that_raises_mid_document_yields_nothing(monkeypatch):
    monkeypatch.setattr(mod, "PDF_SUPPORT", True)
    monkeypatch.setattr(mod, "PdfReader",
                        _reader("One.", RuntimeError("bad page")))
    assert mod.extract_text_from_pdf(b"%PDF") is None


def test_without_pypdf_installed_extraction_yields_nothing(monkeypatch):
    """The dependency is optional, so its absence is a degraded crawl,
    not an import error at startup."""
    monkeypatch.setattr(mod, "PDF_SUPPORT", False)
    assert mod.extract_text_from_pdf(b"%PDF") is None
