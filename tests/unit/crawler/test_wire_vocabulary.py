"""The url->uri convention stops at the wire.

We rename ``url`` to ``uri`` for our OWN identifiers. But a keyword
defined by CSS, by sitemaps.org, or by the Open Graph protocol is not
ours to rename. It is vocabulary we READ.

Six of them had been renamed anyway, and every one failed silently: the
regex or element name simply never matched, so the extraction returned
empty and nothing raised. Coverage did not catch them because these
branches sat in the uncovered half of a 42%-covered function.

Each test below is one of those six.

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

from shared_web_fetcher import (  # noqa: E402
    CrawlMode,
    extract_media_from_html,
    parse_sitemap,
)

BASE = "https://example.com/"


def _loc(item):
    """The located address of a media item, page or feed entry."""
    if isinstance(item, dict):
        return item.get("uri")
    return getattr(item, "uri", None)


def _uris(html: str, mode=CrawlMode.ALL) -> list[str]:
    return [_loc(i) for i in extract_media_from_html(html, BASE, mode=mode)]


# --------------------------------------------------------------------
# CSS: the keyword is url(
# --------------------------------------------------------------------


def test_a_font_face_src_is_extracted():
    """``@font-face { src: url(...) }`` is CSS syntax. Spelled ``uri(``
    the pattern matched no real stylesheet and font discovery was
    dead."""
    html = '<style>@font-face { font-family: X; src: url(/f/x.woff2); }</style>'
    assert "https://example.com/f/x.woff2" in _uris(html)


def test_a_quoted_font_face_src_is_extracted():
    html = '<style>@font-face { src: url("/f/q.woff2"); }</style>'
    assert "https://example.com/f/q.woff2" in _uris(html)


def test_an_inline_background_image_is_extracted():
    html = '<div style="background-image: url(/i/bg.png)"></div>'
    assert "https://example.com/i/bg.png" in _uris(html)


def test_a_background_shorthand_is_extracted():
    html = '<div style="background: url(/i/short.png) no-repeat"></div>'
    assert "https://example.com/i/short.png" in _uris(html)


def test_a_background_image_in_a_style_block_is_extracted():
    html = '<style>.hero { background-image: url(/i/hero.jpg); }</style>'
    assert "https://example.com/i/hero.jpg" in _uris(html)


def test_css_url_matching_is_case_insensitive():
    html = '<div style="BACKGROUND-IMAGE: URL(/i/caps.png)"></div>'
    assert "https://example.com/i/caps.png" in _uris(html)


# --------------------------------------------------------------------
# Open Graph: the property names are spec'd elsewhere
# --------------------------------------------------------------------


def test_og_image_url_is_extracted():
    """``og:image:url`` is an Open Graph property. ``og:image:uri`` is
    not a property at all, so the tag was never matched."""
    html = '<meta property="og:image:url" content="/i/og.png">'
    assert "https://example.com/i/og.png" in _uris(html)


def test_og_video_url_is_extracted():
    html = '<meta property="og:video:url" content="/v/og.mp4">'
    assert "https://example.com/v/og.mp4" in _uris(html)


def test_the_plain_og_image_property_still_works():
    """The unsuffixed form was never renamed — this is the control that
    proves the suffixed test above is testing the fix and not the
    fallback."""
    html = '<meta property="og:image" content="/i/plain.png">'
    assert "https://example.com/i/plain.png" in _uris(html)


def test_a_custom_media_url_meta_tag_is_extracted():
    html = ('<meta property="media:url" content="/m/file.png">'
            '<meta property="media:type" content="image">')
    assert "https://example.com/m/file.png" in _uris(html)


# --------------------------------------------------------------------
# sitemaps.org: the element is <url>
# --------------------------------------------------------------------


SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc><lastmod>2026-01-01</lastmod>
       <priority>0.8</priority></url>
  <url><loc>https://example.com/b</loc></url>
</urlset>"""


def test_a_valid_sitemap_yields_its_entries():
    """The worst of the six. ``<url>`` is the sitemaps.org element, so
    spelled ``uri`` every valid sitemap parsed to ZERO page entries and
    only the ``<sitemap>`` index branch worked — sitemap-driven
    crawling was entirely dead."""
    items, _ = parse_sitemap(SITEMAP, BASE)
    assert [_loc(i) for i in items] == ["https://example.com/a",
                                        "https://example.com/b"]


def test_sitemap_entries_carry_their_metadata():
    items, _ = parse_sitemap(SITEMAP, BASE)
    assert items[0].updated == "2026-01-01"
    assert items[0].priority == 0.8


def test_a_sitemap_entry_without_a_priority_gets_the_default():
    items, _ = parse_sitemap(SITEMAP, BASE)
    assert items[1].priority == 0.5


def test_relative_sitemap_locations_resolve_against_the_base():
    xml = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<url><loc>/relative/page</loc></url></urlset>')
    items, _ = parse_sitemap(xml, BASE)
    assert _loc(items[0]) == "https://example.com/relative/page"


def test_a_sitemap_index_still_yields_sub_sitemaps():
    """The index branch was the only one that worked before, so it is
    the control: the fix must not have broken it."""
    xml = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<sitemap><loc>https://example.com/s1.xml</loc></sitemap>'
           '<sitemap><loc>https://example.com/s2.xml</loc></sitemap>'
           '</sitemapindex>')
    items, subs = parse_sitemap(xml, BASE)
    assert subs == ["https://example.com/s1.xml", "https://example.com/s2.xml"]
    assert items == []


def test_a_malformed_sitemap_yields_nothing_rather_than_raising():
    """A crawler must survive a broken feed; one bad sitemap cannot end
    a crawl."""
    items, subs = parse_sitemap("<not xml at all", BASE)
    assert items == [] and subs == []
