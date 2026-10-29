"""
Tests for async_web_fetcher module.

Tests URI handling, media extraction, robots.txt compliance,
and async fetching functionality.
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
import asyncio
import aiohttp

pytest.importorskip("shared_web_fetcher")

from shared_web_fetcher import (
    CrawlMode,
    MediaItem,
    strip_uri_fragment,
    normalize_link,
    get_media_type_from_extension,
    get_media_type_from_mime,
    extract_media_from_html,
    AsyncWebFetcher,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    AUDIO_EXTENSIONS,
    DEFAULT_CRAWL_DELAY,
    get_last_fetch_error,
    clear_last_fetch_error,
    extract_page_links,
    _extract_page_links_bs4,
)


class TestSharedSession:
    """Shared keepalive session lifecycle (fast-crawl connection reuse)."""

    def test_get_session_is_shared(self):
        async def go():
            f = AsyncWebFetcher()
            s1 = await f._get_session()
            s2 = await f._get_session()
            assert s1 is s2
            await f.aclose()

        asyncio.run(go())

    def test_aclose_closes_and_resets(self):
        async def go():
            f = AsyncWebFetcher()
            s = await f._get_session()
            await f.aclose()
            assert s.closed
            assert f._session is None
            await f.aclose()  # idempotent — must not raise

        asyncio.run(go())

    def test_get_session_rebuilds_after_close(self):
        async def go():
            f = AsyncWebFetcher()
            s1 = await f._get_session()
            await f.aclose()
            s2 = await f._get_session()
            assert s2 is not s1
            assert not s2.closed
            await f.aclose()

        asyncio.run(go())


class TestFastModeIgnoresCrawlDelay:
    """fast_mode must ignore crawl-delay regardless of robots.txt status.

    Regression: the delay was only zeroed on the robots-200 path, so a
    site with no robots.txt (404) fell back to default_crawl_delay (2s)
    and every concurrent fetch wave slept ~2s even under --fast."""

    def test_fast_mode_skips_delay_even_with_default_delay_set(self):
        async def go():
            f = AsyncWebFetcher(fast_mode=True)
            # Simulate the robots-404 fallback: a non-zero per-domain delay.
            f.domain_crawl_delays["x.com"] = 2.0
            f.domain_last_fetched["x.com"] = __import__("time").time()
            t = __import__("time").time()
            await f._enforce_crawl_delay("x.com")
            assert (__import__("time").time() - t) < 0.5, "fast_mode must not sleep"

        asyncio.run(go())

    def test_polite_mode_still_enforces_delay(self):
        async def go():
            import time as _t

            f = AsyncWebFetcher(fast_mode=False, default_crawl_delay=0.3)
            f.domain_crawl_delays["x.com"] = 0.3
            f.domain_last_fetched["x.com"] = _t.time()
            t = _t.time()
            await f._enforce_crawl_delay("x.com")
            assert (_t.time() - t) >= 0.25, "polite mode must still wait"

        asyncio.run(go())


class TestExtractPageLinks:
    """Link extraction for BFS (lxml fast path + BS4 fallback parity)."""

    HTML = (
        '<html><body>'
        '<a href="/a">A</a>'
        '<a href="https://other.com/x">ext</a>'
        '<a href="javascript:void(0)">js</a>'
        '<a href="#frag">frag</a>'
        '<a href="/b" rel="nofollow">nf</a>'
        '<a href="/c#sec">C</a>'
        '</body></html>'
    )

    def test_lxml_extraction_rules(self):

        out = extract_page_links(self.HTML, "https://x.com/")
        assert "https://x.com/a" in out          # relative resolved
        assert "https://other.com/x" in out       # off-domain kept (bridge filters)
        assert "https://x.com/c" in out           # fragment stripped
        assert not any("javascript" in u for u in out)
        assert "https://x.com/b" not in out       # rel=nofollow dropped
        assert not any("#" in u for u in out)

    def test_lxml_matches_bs4_fallback(self):

        assert (
            extract_page_links(self.HTML, "https://x.com/")
            == _extract_page_links_bs4(self.HTML, "https://x.com/")
        )

    def test_anchor_text_mode(self):

        out = extract_page_links(self.HTML, "https://x.com/", extract_anchor_text=True)
        a = next(d for d in out if d["uri"] == "https://x.com/a")
        assert a["anchor_text"] == "A"

    def test_malformed_html_falls_back_gracefully(self):

        # Empty / whitespace markup makes lxml.fromstring raise — the
        # helper must fall back, not crash.
        assert extract_page_links("   ", "https://x.com/") == []


class TestMetaRefresh:
    """<meta http-equiv=\"refresh\"> BFS follow — unturf.com root page
    serves HTTP 200 with a meta-refresh in HTML (Caddy static-page
    setup), so httpx follow_redirects (Location-header only) misses
    it. Crawler recovers the target from the HTML."""

    UNTURF_HTML = (
        '<!DOCTYPE html><html lang="en"><head>'
        '<meta http-equiv="Refresh" content="0; uri=\'/let-us-define-a-permacomputer/\'" />'
        '</head><body></body></html>'
    )

    def test_lxml_extracts_meta_refresh(self):

        out = extract_page_links(self.UNTURF_HTML, "https://www.unturf.com/")
        assert out == ["https://www.unturf.com/let-us-define-a-permacomputer/"]

    def test_bs4_extracts_meta_refresh(self):

        out = _extract_page_links_bs4(self.UNTURF_HTML, "https://www.unturf.com/")
        assert out == ["https://www.unturf.com/let-us-define-a-permacomputer/"]

    def test_meta_refresh_variants(self):
        """Bare URI, no leading whitespace, uppercase, no space after ;."""

        # Bare URI, no quotes
        html = '<html><head><meta http-equiv="refresh" content="5;uri=https://ex.com/t"></head></html>'
        assert extract_page_links(html, "https://a.com/") == ["https://ex.com/t"]

        # Uppercase http-equiv value
        html = '<html><head><meta http-equiv="REFRESH" content="0; URI=/t"></head></html>'
        assert extract_page_links(html, "https://a.com/") == ["https://a.com/t"]

    def test_non_refresh_meta_ignored(self):

        html = (
            '<html><head><meta http-equiv="content-type" content="text/html">'
            '</head><body><a href="/x">x</a></body></html>'
        )
        assert extract_page_links(html, "https://a.com/") == ["https://a.com/x"]

    def test_meta_refresh_ordered_before_anchors(self):
        """Meta-refresh target must sort before ordinary anchors so BFS
        enqueues the redirect target first when a page has both."""

        html = (
            '<html><head><meta http-equiv="Refresh" content="0;uri=/target">'
            '</head><body><a href="/x">x</a></body></html>'
        )
        assert extract_page_links(html, "https://a.com/") == [
            "https://a.com/target",
            "https://a.com/x",
        ]

    def test_meta_refresh_anchor_text_mode(self):

        html = '<html><head><meta http-equiv="Refresh" content="0;uri=/t"></head></html>'
        out = extract_page_links(html, "https://a.com/", extract_anchor_text=True)
        assert out == [{"uri": "https://a.com/t", "anchor_text": ""}]

    def test_meta_refresh_lxml_bs4_parity(self):
        html = (
            '<html><head><meta http-equiv="Refresh" content="0;uri=/target">'
            '</head><body><a href="/x">x</a></body></html>'
        )
        assert (
            extract_page_links(html, "https://a.com/")
            == _extract_page_links_bs4(html, "https://a.com/")
        )

    def test_meta_refresh_bad_scheme_dropped(self):

        # ftp:// isn't followable by the crawler; skip.
        html = '<html><head><meta http-equiv="Refresh" content="0;uri=ftp://x.com/y"></head></html>'
        assert extract_page_links(html, "https://a.com/") == []


class TestCrawlMode:
    """Test CrawlMode enum."""

    def test_crawl_mode_values(self):
        """Test all crawl mode values exist."""
        assert CrawlMode.TEXT.value == "text"
        assert CrawlMode.IMAGES.value == "images"
        assert CrawlMode.VIDEOS.value == "videos"
        assert CrawlMode.MEDIA.value == "media"
        assert CrawlMode.ALL.value == "all"

    def test_crawl_mode_from_string(self):
        """Test creating CrawlMode from string."""
        assert CrawlMode("text") == CrawlMode.TEXT
        assert CrawlMode("images") == CrawlMode.IMAGES
        assert CrawlMode("media") == CrawlMode.MEDIA


class TestMediaItem:
    """Test MediaItem dataclass."""

    def test_media_item_creation(self):
        """Test creating a MediaItem."""
        item = MediaItem(
            uri="https://example.com/image.jpg",
            source_page="https://example.com/",
            media_type="image",
            mime_type="image/jpeg",
        )
        assert item.uri == "https://example.com/image.jpg"
        assert item.source_page == "https://example.com/"
        assert item.media_type == "image"
        assert item.mime_type == "image/jpeg"
        assert item.discovered_at is not None

    def test_media_item_optional_fields(self):
        """Test MediaItem with optional fields."""
        item = MediaItem(
            uri="https://example.com/video.mp4",
            source_page="https://example.com/",
            media_type="video",
            alt_text="A video",
            width=1920,
            height=1080,
        )
        assert item.alt_text == "A video"
        assert item.width == 1920
        assert item.height == 1080


class TestURIHandling:
    """Test URI handling functions."""

    def test_strip_uri_fragment_basic(self):
        """Test stripping fragment from URI."""
        uri = "https://example.com/page#section"
        result = strip_uri_fragment(uri)
        assert result == "https://example.com/page"

    def test_strip_uri_fragment_no_fragment(self):
        """Test URI without fragment."""
        uri = "https://example.com/page"
        result = strip_uri_fragment(uri)
        assert result == "https://example.com/page"

    def test_strip_uri_fragment_with_query(self):
        """Test URI with query and fragment."""
        uri = "https://example.com/page?q=test#section"
        result = strip_uri_fragment(uri)
        assert result == "https://example.com/page?q=test"

    def test_strip_uri_fragment_empty(self):
        """Test empty URI."""
        assert strip_uri_fragment("") == ""
        assert strip_uri_fragment(None) is None

    def test_normalize_link_dict(self):
        """Test normalizing link from dict."""
        link = {"uri": "https://example.com/page#section", "anchor_text": "Click here"}
        uri, anchor = normalize_link(link)
        assert uri == "https://example.com/page"
        assert anchor == "Click here"

    def test_normalize_link_string(self):
        """Test normalizing link from string."""
        uri, anchor = normalize_link("https://example.com/page#section")
        assert uri == "https://example.com/page"
        assert anchor == ""


class TestMediaTypeDetection:
    """Test media type detection functions."""

    def test_get_media_type_from_extension_image(self):
        """Test detecting image from extension."""
        assert get_media_type_from_extension("https://example.com/photo.jpg") == "image"
        assert get_media_type_from_extension("https://example.com/photo.png") == "image"
        assert get_media_type_from_extension("https://example.com/photo.webp") == "image"
        assert get_media_type_from_extension("https://example.com/photo.gif") == "image"

    def test_get_media_type_from_extension_video(self):
        """Test detecting video from extension."""
        assert get_media_type_from_extension("https://example.com/video.mp4") == "video"
        assert get_media_type_from_extension("https://example.com/video.webm") == "video"
        assert get_media_type_from_extension("https://example.com/video.mov") == "video"

    def test_get_media_type_from_extension_audio(self):
        """Test detecting audio from extension."""
        assert get_media_type_from_extension("https://example.com/audio.mp3") == "audio"
        assert get_media_type_from_extension("https://example.com/audio.wav") == "audio"
        assert get_media_type_from_extension("https://example.com/audio.ogg") == "audio"

    def test_get_media_type_from_extension_code(self):
        """Test detecting code from extension."""
        assert get_media_type_from_extension("https://example.com/file.txt") == "code"
        assert get_media_type_from_extension("https://example.com/script.py") == "code"
        assert get_media_type_from_extension("https://example.com/app.js") == "code"

    def test_get_media_type_from_extension_unknown(self):
        """Test unknown extension."""
        assert get_media_type_from_extension("https://example.com/page.html") is None
        assert get_media_type_from_extension("https://example.com/file.xyz") is None

    def test_get_media_type_from_mime_image(self):
        """Test detecting image from MIME type."""
        assert get_media_type_from_mime("image/jpeg") == "image"
        assert get_media_type_from_mime("image/png") == "image"
        assert get_media_type_from_mime("image/webp") == "image"

    def test_get_media_type_from_mime_video(self):
        """Test detecting video from MIME type."""
        assert get_media_type_from_mime("video/mp4") == "video"
        assert get_media_type_from_mime("video/webm") == "video"

    def test_get_media_type_from_mime_audio(self):
        """Test detecting audio from MIME type."""
        assert get_media_type_from_mime("audio/mpeg") == "audio"
        assert get_media_type_from_mime("audio/wav") == "audio"

    def test_get_media_type_from_mime_code(self):
        """Test detecting code from MIME type."""
        assert get_media_type_from_mime("application/json") == "code"
        assert get_media_type_from_mime("text/plain") == "code"
        assert get_media_type_from_mime("application/javascript") == "code"

    def test_get_media_type_from_mime_unknown(self):
        """Test unknown MIME type."""
        assert get_media_type_from_mime("text/html") is None
        assert get_media_type_from_mime("application/octet-stream") is None


class TestMediaExtraction:
    """Test media extraction from HTML."""

    def test_extract_images_from_html(self):
        """Test extracting images from HTML."""
        html = '''
        <html>
        <body>
            <img src="/images/photo.jpg" alt="A photo">
            <img src="https://cdn.example.com/logo.png" alt="Logo">
        </body>
        </html>
        '''
        media = extract_media_from_html(html, "https://example.com/", CrawlMode.IMAGES)

        assert len(media) >= 1
        uris = [m['uri'] for m in media]
        # Should have resolved relative URI
        assert any('photo.jpg' in uri for uri in uris)

    def test_extract_videos_from_html(self):
        """Test extracting videos from HTML."""
        html = '''
        <html>
        <body>
            <video src="/videos/movie.mp4"></video>
            <source src="/videos/clip.webm" type="video/webm">
        </body>
        </html>
        '''
        media = extract_media_from_html(html, "https://example.com/", CrawlMode.VIDEOS)

        uris = [m['uri'] for m in media]
        assert any('movie.mp4' in uri or 'clip.webm' in uri for uri in uris)

    def test_extract_media_filters_by_mode(self):
        """Test that extraction respects crawl mode."""
        html = '''
        <html>
        <body>
            <img src="/photo.jpg">
            <video src="/video.mp4"></video>
            <audio src="/audio.mp3"></audio>
        </body>
        </html>
        '''

        # Images only
        images = extract_media_from_html(html, "https://example.com/", CrawlMode.IMAGES)
        image_uris = [m['uri'] for m in images]
        assert any('photo.jpg' in uri for uri in image_uris)
        assert not any('video.mp4' in uri for uri in image_uris)

        # Videos only
        videos = extract_media_from_html(html, "https://example.com/", CrawlMode.VIDEOS)
        video_uris = [m['uri'] for m in videos]
        assert any('video.mp4' in uri for uri in video_uris)


class TestAsyncWebFetcher:
    """Test AsyncWebFetcher class."""

    def test_fetcher_initialization(self):
        """Test AsyncWebFetcher initialization."""
        fetcher = AsyncWebFetcher(
            user_agent="TestBot/1.0",
            default_crawl_delay=1.0,
        )
        assert fetcher.user_agent == "TestBot/1.0"
        assert fetcher.default_crawl_delay == 1.0

    def test_fetcher_default_values(self):
        """Test AsyncWebFetcher default values."""
        fetcher = AsyncWebFetcher()
        assert fetcher.default_crawl_delay == DEFAULT_CRAWL_DELAY

    def test_get_domain(self):
        """Test domain extraction."""
        fetcher = AsyncWebFetcher()
        assert fetcher._get_domain("https://example.com/page") == "example.com"
        assert fetcher._get_domain("https://sub.example.com/page") == "sub.example.com"

    def test_simple_stem(self):
        """Test simple word stemming."""
        fetcher = AsyncWebFetcher()

        # Test stemming - actual behavior may vary
        stem = fetcher._simple_stem("running")
        assert "run" in stem  # Should contain "run"

        # Short words should not be stemmed
        assert fetcher._simple_stem("go") == "go"

    def test_extract_text_content(self):
        """Test text extraction from HTML."""
        fetcher = AsyncWebFetcher()
        html = '''
        <html>
        <head><title>Test Page</title></head>
        <body>
            <h1>Hello World</h1>
            <p>This is a test paragraph.</p>
            <script>alert('ignored');</script>
            <style>.hidden { display: none; }</style>
        </body>
        </html>
        '''
        text = fetcher.extract_text_content(html)

        assert "Hello World" in text
        assert "test paragraph" in text
        assert "alert" not in text  # Script content should be removed
        assert ".hidden" not in text  # Style content should be removed

    def test_extract_title(self):
        """Test title extraction from HTML."""
        fetcher = AsyncWebFetcher()
        html = '<html><head><title>Page Title</title></head><body></body></html>'
        title = fetcher.extract_title(html)
        assert title == "Page Title"

    def test_extract_title_missing(self):
        """Test title extraction when missing."""
        fetcher = AsyncWebFetcher()
        html = '<html><head></head><body></body></html>'
        title = fetcher.extract_title(html)
        # Returns "Untitled" when no title present
        assert title == "Untitled" or title == ""


class TestAsyncWebFetcherScoring:
    """Test link and page scoring functionality."""

    def test_score_link_keyword_match(self):
        """Test link scoring with keyword matches."""
        fetcher = AsyncWebFetcher()

        # Link with keyword in URI
        score = fetcher._score_link(
            "https://example.com/pricing",
            "View Pricing",
            query_keywords=["pricing", "cost"],
        )
        assert score > 0  # Should have positive score

        # Link without keywords
        score_none = fetcher._score_link(
            "https://example.com/about",
            "About Us",
            query_keywords=["pricing", "cost"],
        )
        assert score > score_none  # Keyword match should score higher

    def test_score_link_anchor_text(self):
        """Test link scoring with anchor text."""
        fetcher = AsyncWebFetcher()

        # Anchor text with keyword
        score = fetcher._score_link(
            "https://example.com/page",
            "Our Pricing Plans",
            query_keywords=["pricing"],
        )
        assert score > 0

    def test_score_page_keyword_match(self):
        """Test page scoring with keyword matches."""
        fetcher = AsyncWebFetcher()

        page_data = {
            "uri": "https://example.com/pricing",
            "title": "Pricing Plans",
            "extracted_text": "Our pricing is competitive. See our cost breakdown.",
        }

        score = fetcher._score_page(
            page_data,
            query_keywords=["pricing", "cost"],
        )
        assert score > 0


class TestAsyncWebFetcherAsync:
    """Test async methods of AsyncWebFetcher."""

    @pytest.mark.asyncio
    async def test_can_fetch_respects_robots(self):
        """Test that _can_fetch respects robots.txt."""
        fetcher = AsyncWebFetcher()

        # Mock the robots.txt fetch to return a parser that disallows
        with patch.object(fetcher, '_fetch_robots_txt', new_callable=AsyncMock) as mock_robots:
            mock_parser = MagicMock()
            mock_parser.can_fetch.return_value = False
            mock_robots.return_value = mock_parser

            result = await fetcher._can_fetch("https://example.com/blocked")
            assert result is False

    @pytest.mark.asyncio
    async def test_can_fetch_allows_when_parser_allows(self):
        """Test _can_fetch when robots parser allows."""
        fetcher = AsyncWebFetcher()

        with patch.object(fetcher, '_fetch_robots_txt', new_callable=AsyncMock) as mock_robots:
            mock_parser = MagicMock()
            mock_parser.can_fetch.return_value = True
            mock_robots.return_value = mock_parser

            result = await fetcher._can_fetch("https://example.com/allowed")
            assert result is True

    @pytest.mark.asyncio
    async def test_enforce_crawl_delay(self):
        """Test crawl delay enforcement."""
        fetcher = AsyncWebFetcher(default_crawl_delay=0.1)
        domain = "example.com"

        # First request should not delay
        start = asyncio.get_event_loop().time()
        await fetcher._enforce_crawl_delay(domain)
        first_time = asyncio.get_event_loop().time() - start

        # Second request should delay
        start = asyncio.get_event_loop().time()
        await fetcher._enforce_crawl_delay(domain)
        second_time = asyncio.get_event_loop().time() - start

        # Second call should have some delay (at least part of 0.1s)
        assert second_time >= 0.05  # Allow some tolerance

    @pytest.mark.asyncio
    async def test_check_media_uri_signature(self):
        """Test check_media_uri function signature."""
        fetcher = AsyncWebFetcher()

        # Verify the method exists and has correct signature
        import inspect
        sig = inspect.signature(fetcher.check_media_uri)
        params = list(sig.parameters.keys())

        # Should have uri parameter
        assert 'uri' in params or len(params) >= 1


class TestErrorHandling:
    """Test error handling functionality."""

    def test_get_last_fetch_error_initially_none(self):
        """Test that last fetch error is None initially."""
        clear_last_fetch_error()
        error = get_last_fetch_error()
        assert error is None

    def test_clear_last_fetch_error(self):
        """Test clearing the last fetch error."""
        clear_last_fetch_error()
        assert get_last_fetch_error() is None


class TestExtensionSets:
    """Test extension and MIME type sets."""

    def test_image_extensions(self):
        """Test image extension set."""
        assert '.jpg' in IMAGE_EXTENSIONS
        assert '.jpeg' in IMAGE_EXTENSIONS
        assert '.png' in IMAGE_EXTENSIONS
        assert '.gif' in IMAGE_EXTENSIONS
        assert '.webp' in IMAGE_EXTENSIONS
        assert '.svg' in IMAGE_EXTENSIONS

    def test_video_extensions(self):
        """Test video extension set."""
        assert '.mp4' in VIDEO_EXTENSIONS
        assert '.webm' in VIDEO_EXTENSIONS
        assert '.mov' in VIDEO_EXTENSIONS
        assert '.avi' in VIDEO_EXTENSIONS

    def test_audio_extensions(self):
        """Test audio extension set."""
        assert '.mp3' in AUDIO_EXTENSIONS
        assert '.wav' in AUDIO_EXTENSIONS
        assert '.ogg' in AUDIO_EXTENSIONS
        assert '.flac' in AUDIO_EXTENSIONS


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
