"""Everything ``extract_media_from_html`` pulls off a page.

It is the widest function in the fetcher — every element and attribute
a browser might load an asset from, plus mode filtering, deduplication,
surrounding-context capture and page-level metadata. It needs no
network at all; it was simply never covered past the wire-vocabulary
cases.

This suite is written as characterization: it pins what the function
does today, section by section, so the shape of it can be changed
without changing what it finds.

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
    MediaMetadata,
    extract_media_from_html,
)

BASE = "https://example.com/page/"


def _loc(item):
    """The located address of a media item, page or feed entry."""
    if isinstance(item, dict):
        return item.get("uri")
    return getattr(item, "uri", None)


def _detail(item):
    """The linked detail page recorded on a media item."""
    return item.get("detail_page_uri")


def items(html, mode=CrawlMode.ALL, base=BASE):
    return extract_media_from_html(html, base, mode=mode)


def uris(html, mode=CrawlMode.ALL, base=BASE):
    return [_loc(i) for i in items(html, mode=mode, base=base)]


def one(html, mode=CrawlMode.ALL, base=BASE):
    got = items(html, mode=mode, base=base)
    assert len(got) == 1, f"expected exactly one item, got {len(got)}"
    return got[0]


def doc(body="", head=""):
    return f"<html><head>{head}</head><body>{body}</body></html>"


# --------------------------------------------------------------------
# <img>
# --------------------------------------------------------------------


def test_an_image_src_is_extracted():
    assert uris('<img src="/i/a.png">') == ["https://example.com/i/a.png"]


def test_a_relative_src_resolves_against_the_page_not_the_host():
    """urljoin against the full page address — /page/ is the directory,
    so a bare filename belongs to it."""
    assert uris('<img src="b.png">') == ["https://example.com/page/b.png"]


def test_an_absolute_src_is_left_alone():
    assert uris('<img src="https://cdn.example.net/x.png">') == [
        "https://cdn.example.net/x.png"]


def test_a_data_uri_image_is_skipped():
    """Inline bytes are already in hand; there is nothing to fetch and
    the string can be megabytes."""
    assert uris('<img src="data:image/png;base64,iVBORw0KGgo=">') == []


def test_an_image_without_a_src_yields_nothing():
    assert uris('<img alt="no source">') == []


def test_alt_and_title_are_captured():
    got = one('<img src="/a.png" alt="A cat" title="Cat photo">')
    assert got["alt_text"] == "A cat"
    assert got["title"] == "Cat photo"


def test_numeric_dimensions_are_captured():
    got = one('<img src="/a.png" width="800" height="600">')
    assert (got["width"], got["height"]) == (800, 600)


def test_non_numeric_dimensions_are_discarded():
    """A CSS width like "100%" is not a pixel count; storing it as one
    would poison any size-based filtering downstream."""
    got = one('<img src="/a.png" width="100%" height="auto">')
    assert (got["width"], got["height"]) == (None, None)


def test_every_srcset_candidate_is_extracted():
    """A responsive image is several files; a crawl that takes only the
    src misses the resolutions that matter."""
    html = '<img src="/a.png" srcset="/a-1x.png 1x, /a-2x.png 2x">'
    assert set(uris(html)) == {"https://example.com/a-1x.png",
                               "https://example.com/a-2x.png",
                               "https://example.com/a.png"}


def test_a_srcset_size_descriptor_is_not_part_of_the_address():
    assert uris('<img srcset="/a.png 1024w">') == ["https://example.com/a.png"]


@pytest.mark.parametrize("attr", ["data-src", "data-lazy-src", "data-original"])
def test_lazy_loading_attributes_are_extracted(attr):
    """A lazy-loaded page has no real src until script runs, so these
    attributes are the only address a crawler ever sees."""
    assert uris(f'<img {attr}="/lazy.png">') == ["https://example.com/lazy.png"]


def test_a_lazy_srcset_is_split_like_a_real_one():
    html = '<img data-srcset="/l-1x.png 1x, /l-2x.png 2x">'
    assert set(uris(html)) == {"https://example.com/l-1x.png",
                               "https://example.com/l-2x.png"}


# --------------------------------------------------------------------
# <picture>, <video>, <audio>
# --------------------------------------------------------------------


def test_picture_sources_are_extracted():
    html = ('<picture><source srcset="/wide.png 1200w">'
            '<source srcset="/narrow.png 400w"><img src="/fallback.png">'
            '</picture>')
    assert set(uris(html)) >= {"https://example.com/wide.png",
                               "https://example.com/narrow.png",
                               "https://example.com/fallback.png"}


def test_a_video_src_is_extracted():
    got = one('<video src="/v/clip.mp4" title="A clip"></video>')
    assert _loc(got) == "https://example.com/v/clip.mp4"
    assert got["media_type"] == "video"


def test_a_video_poster_is_extracted_as_an_image():
    """The poster is a still frame — an image asset, not a video."""
    got = items('<video src="/v/clip.mp4" poster="/v/poster.jpg"></video>')
    poster = [i for i in got if _loc(i).endswith("poster.jpg")]
    assert poster and poster[0]["media_type"] == "image"


def test_a_video_poster_is_skipped_when_images_are_not_wanted():
    got = uris('<video src="/v/clip.mp4" poster="/v/poster.jpg"></video>',
               mode=CrawlMode.VIDEOS)
    assert "https://example.com/v/poster.jpg" not in got


def test_sources_nested_in_a_video_are_extracted():
    html = ('<video><source src="/v/a.webm"><source src="/v/a.mp4"></video>')
    assert set(uris(html)) == {"https://example.com/v/a.webm",
                               "https://example.com/v/a.mp4"}


def test_an_audio_src_is_extracted():
    got = one('<audio src="/a/song.mp3"></audio>')
    assert got["media_type"] == "audio"


def test_sources_nested_in_an_audio_are_extracted():
    html = '<audio><source src="/a/song.ogg"></audio>'
    assert uris(html) == ["https://example.com/a/song.ogg"]


# --------------------------------------------------------------------
# Links, stylesheets, scripts
# --------------------------------------------------------------------


def test_a_link_to_an_image_file_is_extracted():
    got = one('<a href="/full/photo.jpg">See full size</a>')
    assert got["media_type"] == "image"
    assert got["alt_text"] == "See full size"


def test_a_link_to_an_ordinary_page_is_not_media():
    assert uris('<a href="/about">About us</a>') == []


@pytest.mark.parametrize("href,kind", [
    ("/app.js", "code"),
    ("/f/font.woff2", "font"),
    ("/s/site.css", "style"),
])
def test_code_font_and_style_links_are_always_collected(href, kind):
    """Collected regardless of mode: they are what a page NEEDS to
    render, not media a mode filter is about."""
    got = one(f'<a href="{href}">x</a>', mode=CrawlMode.IMAGES)
    assert got["media_type"] == kind


def test_a_stylesheet_link_is_extracted():
    got = one('<link rel="stylesheet" href="/s/site.css">')
    assert got["media_type"] == "style"


def test_a_preloaded_font_is_extracted():
    got = one('<link rel="preload" as="font" href="/f/x.woff2">')
    assert got["media_type"] == "font"


def test_a_preloaded_stylesheet_is_extracted():
    got = one('<link rel="preload" as="style" href="/s/x.css">')
    assert got["media_type"] == "style"


def test_a_link_is_classified_by_extension_when_rel_says_nothing():
    got = one('<link rel="alternate" href="/f/y.woff">')
    assert got["media_type"] == "font"


def test_an_unremarkable_link_tag_is_ignored():
    assert uris('<link rel="canonical" href="/page/">') == []


def test_a_script_src_is_extracted():
    got = one('<script src="/js/app.js"></script>')
    assert got["media_type"] == "code"


def test_an_inline_script_yields_nothing():
    assert uris("<script>var x = 1;</script>") == []


# --------------------------------------------------------------------
# Mode filtering
# --------------------------------------------------------------------


MIXED = ('<img src="/i.png"><video src="/v.mp4"></video>'
         '<audio src="/a.mp3"></audio>')


def test_image_mode_takes_only_images():
    assert uris(MIXED, mode=CrawlMode.IMAGES) == ["https://example.com/i.png"]


def test_video_mode_takes_only_videos():
    assert uris(MIXED, mode=CrawlMode.VIDEOS) == ["https://example.com/v.mp4"]


def test_media_mode_takes_all_three():
    assert len(uris(MIXED, mode=CrawlMode.MEDIA)) == 3


def test_audio_needs_more_than_image_or_video_mode():
    """There is no AUDIO mode; audio rides on MEDIA and ALL."""
    assert "https://example.com/a.mp3" not in uris(MIXED, mode=CrawlMode.IMAGES)
    assert "https://example.com/a.mp3" not in uris(MIXED, mode=CrawlMode.VIDEOS)


# --------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------


def test_the_same_address_twice_yields_one_item():
    assert uris('<img src="/a.png"><img src="/a.png">') == [
        "https://example.com/a.png"]


def test_a_relative_and_absolute_form_of_one_address_yield_one_item():
    """Dedup happens after resolution — otherwise the same file is
    fetched, hashed and stored twice under two spellings."""
    html = '<img src="/a.png"><img src="https://example.com/a.png">'
    assert uris(html) == ["https://example.com/a.png"]


def test_one_address_reached_two_ways_yields_one_item():
    html = '<a href="/x.png">link</a><img src="/x.png">'
    assert uris(html) == ["https://example.com/x.png"]


# --------------------------------------------------------------------
# Surrounding context
# --------------------------------------------------------------------


def test_a_figure_caption_is_captured():
    html = ('<figure><img src="/a.png">'
            '<figcaption>A caption</figcaption></figure>')
    assert one(html)["figure_caption"] == "A caption"


def test_wrapping_link_text_is_captured():
    got = one('<a href="/about"><img src="/a.png">Click through</a>')
    assert got["link_text"] == "Click through"


def test_wrapping_link_title_is_captured():
    got = one('<a href="/about" title="Tooltip text"><img src="/a.png"></a>')
    assert got["link_title"] == "Tooltip text"


def test_link_text_identical_to_the_alt_is_not_repeated():
    got = one('<a href="/about"><img src="/a.png" alt="Same"></a>')
    assert got["link_text"] is None


def test_a_preceding_heading_is_captured():
    got = one('<h2>Section heading</h2><p>x</p><img src="/a.png">')
    assert got["nearby_heading"] == "Section heading"


def test_the_nearest_heading_wins_over_an_earlier_one():
    html = ("<h1>Page top</h1><p>x</p>"
            "<h2>Closer heading</h2><img src='/a.png'>")
    assert one(html)["nearby_heading"] == "Closer heading"


def test_an_image_with_no_surroundings_has_no_context():
    got = one('<img src="/a.png">')
    assert got["figure_caption"] is None
    assert got["link_text"] is None
    assert got["nearby_heading"] is None


# --------------------------------------------------------------------
# Page-level metadata, attached to every item
# --------------------------------------------------------------------


def test_the_page_title_rides_along():
    got = one(doc('<img src="/a.png">', head="<title>My Gallery</title>"))
    assert got["page_title"] == "My Gallery"


def test_the_page_description_rides_along():
    html = doc('<img src="/a.png">',
               head='<meta name="description" content="All about cats">')
    assert one(html)["page_description"] == "All about cats"


def test_og_description_also_supplies_the_page_description():
    html = doc('<img src="/a.png">',
               head='<meta property="og:description" content="OG words">')
    assert one(html)["page_description"] == "OG words"


def test_page_keywords_ride_along():
    html = doc('<img src="/a.png">',
               head='<meta name="keywords" content="cats, photos">')
    assert one(html)["page_keywords"] == "cats, photos"


def test_the_page_body_text_rides_along():
    """This is what makes a blog image findable by the words of the post
    it illustrates."""
    html = doc('<p>A distinctive sentence.</p><img src="/a.png">')
    assert "distinctive sentence" in one(html)["page_content"]


def test_site_chrome_is_excluded_from_the_page_text():
    html = doc("<nav>Home About</nav><p>Real words.</p>"
               "<footer>Copyright</footer><img src='/a.png'>")
    content = one(html)["page_content"]
    assert "Real words." in content
    assert "Copyright" not in content


def test_an_image_inside_stripped_chrome_is_still_extracted():
    """The body copy used for text is a COPY. Decomposing nav out of the
    original would silently drop every header logo and nav icon."""
    html = doc('<header><img src="/logo.png"></header><p>Body.</p>')
    assert "https://example.com/logo.png" in uris(html)


def test_the_source_page_is_recorded_on_every_item():
    assert one('<img src="/a.png">')["source_page"] == BASE


# --------------------------------------------------------------------
# Detail-page linking
# --------------------------------------------------------------------


def test_a_thumbnail_wrapped_in_an_internal_link_records_a_detail_page():
    """Gallery shape: the thumbnail links to a page holding the
    full-res. That address is what resolve_canonical_image then
    follows."""
    got = one('<a href="/photo/42"><img src="/t/42.jpg"></a>')
    assert _detail(got) == "https://example.com/photo/42"


def test_a_thumbnail_linking_straight_to_an_image_records_no_detail_page():
    """The href IS the full-res; there is no page to visit."""
    got = items('<a href="/full/42.jpg"><img src="/t/42.jpg"></a>')
    thumb = [i for i in got if _loc(i).endswith("t/42.jpg")][0]
    assert _detail(thumb) is None


def test_an_offsite_link_records_no_detail_page():
    """Following it would walk the crawl off the domain it was pointed
    at."""
    got = one('<a href="https://other.example.org/p/1"><img src="/t.jpg"></a>')
    assert _detail(got) is None


def test_an_unwrapped_image_records_no_detail_page():
    assert _detail(one('<img src="/a.png">')) is None


# --------------------------------------------------------------------
# Meta-tag media
# --------------------------------------------------------------------


def test_a_custom_media_url_meta_pair_is_extracted():
    html = doc(head='<meta property="media:url" content="/m/f.mp4">'
                    '<meta property="media:type" content="video">')
    got = one(html)
    assert got["media_type"] == "video"
    assert _loc(got) == "https://example.com/m/f.mp4"


def test_a_custom_media_url_defaults_to_image():
    html = doc(head='<meta property="media:url" content="/m/f.png">')
    assert one(html)["media_type"] == "image"


def test_a_custom_media_url_honours_the_mode():
    html = doc(head='<meta property="media:url" content="/m/f.mp4">'
                    '<meta property="media:type" content="video">')
    assert uris(html, mode=CrawlMode.IMAGES) == []


def test_og_title_names_an_og_image():
    html = doc(head='<meta property="og:title" content="The Page Name">'
                    '<meta property="og:image" content="/og.png">')
    assert one(html)["title"] == "The Page Name"


def test_og_image_alt_supplies_the_alt_text():
    html = doc(head='<meta property="og:image:alt" content="Described">'
                    '<meta property="og:image" content="/og.png">')
    assert one(html)["alt_text"] == "Described"


def test_a_twitter_card_image_is_extracted():
    html = doc(head='<meta name="twitter:image" content="/tw.png">')
    assert uris(html) == ["https://example.com/tw.png"]


def test_a_twitter_player_is_extracted_as_video():
    html = doc(head='<meta name="twitter:player" content="/tw.mp4">')
    assert one(html)["media_type"] == "video"


def test_a_meta_tag_with_no_content_yields_nothing():
    assert uris(doc(head='<meta property="og:image" content="">')) == []


# --------------------------------------------------------------------
# The accumulated searchable text
# --------------------------------------------------------------------


def test_searchable_text_gathers_every_associated_string():
    """"Never clobber, always append" — an image should be findable by
    ANY text near it, not only its alt."""
    html = doc('<figure><a href="/p/1" title="Link tip">'
               '<img src="/a.png" alt="Alt words" title="Title words"></a>'
               '<figcaption>Caption words</figcaption></figure>',
               head="<title>Page words</title>")
    text = one(html)["searchable_text"]
    for word in ("Alt words", "Title words", "Caption words",
                 "Link tip", "Page words"):
        assert word in text


def test_the_metadata_breakdown_is_exported():
    got = one('<img src="/a.png" alt="Alt" title="Title">')
    meta = got["metadata"]
    assert meta["alt_texts"] == ["Alt"]
    assert meta["titles"][0] == "Title"


def test_an_explicit_title_outranks_the_page_title():
    html = doc('<img src="/a.png" title="Specific">',
               head="<title>Generic Site</title>")
    assert one(html)["title"] == "Specific"


def test_the_page_title_is_the_last_resort_for_a_bare_image():
    html = doc('<img src="/a.png">', head="<title>Generic Site</title>")
    assert one(html)["title"] == "Generic Site"


# --------------------------------------------------------------------
# Degenerate input
# --------------------------------------------------------------------


def test_an_empty_document_yields_nothing():
    assert uris("") == []


def test_a_document_with_no_media_yields_nothing():
    assert uris(doc("<p>Just words.</p>", head="<title>T</title>")) == []


def test_unparseable_markup_does_not_raise():
    assert isinstance(items("<img src=<<<>>> <<"), list)


# --------------------------------------------------------------------
# MediaMetadata, the accumulator underneath
# --------------------------------------------------------------------


def test_metadata_keeps_the_first_of_each_kind_first():
    m = MediaMetadata()
    m.add_img_alt("first")
    m.add_img_alt("second")
    assert m.get_best_alt() == "first"


def test_metadata_never_stores_a_duplicate():
    m = MediaMetadata()
    m.add_img_alt("same")
    m.add_img_alt("same")
    assert m.alt_texts == ["same"]


def test_metadata_ignores_blank_values():
    m = MediaMetadata()
    m.add_img_alt("   ")
    m.add_img_title("")
    assert m.to_searchable_text() == ""


def test_metadata_strips_whitespace():
    m = MediaMetadata()
    m.add_img_alt("  padded  ")
    assert m.alt_texts == ["padded"]


def test_metadata_truncates_a_runaway_value():
    m = MediaMetadata()
    m.add_img_alt("x" * 900)
    assert len(m.alt_texts[0]) == 500


def test_the_best_title_falls_through_to_the_next_source():
    m = MediaMetadata()
    m.add_figcaption("A caption")
    assert m.get_best_title() == "A caption"


def test_the_best_alt_falls_back_to_the_best_title():
    m = MediaMetadata()
    m.add_img_title("A title")
    assert m.get_best_alt() == "A title"


def test_an_empty_accumulator_has_no_best_anything():
    m = MediaMetadata()
    assert m.get_best_title() is None
    assert m.get_best_alt() is None


def test_searchable_text_joins_with_a_separator():
    m = MediaMetadata()
    m.add_img_title("one")
    m.add_img_alt("two")
    assert m.to_searchable_text() == "one | two"


def test_searchable_text_dedupes_across_kinds():
    """The same string arriving as both an alt and a caption is one
    piece of information, not two."""
    m = MediaMetadata()
    m.add_img_alt("repeated")
    m.add_figcaption("repeated")
    assert m.to_searchable_text() == "repeated"
