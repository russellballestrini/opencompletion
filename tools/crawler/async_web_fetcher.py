#!/usr/bin/env python3
"""
Async Web Fetcher for Discord Bot
Ethical web scraping with robots.txt compliance, async/await compatible

Supports multiple crawl modes:
- text: Extract text content (default, current behavior)
- images: Collect images only
- videos: Collect videos only
- media: Collect all media (images + videos + audio)
- all: Uber crawl - text + all media, full domain slurp
"""

import os
import io
import hashlib
import logging
import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, List, Tuple, Set, Any
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser
import time

# This file is SHARED VERBATIM across four repos: arborist,
# agents.ai.unturf.com, crawler.unturf.com and neopig. Byte-identical is
# the point — a defect fixed in one copy used to stay live in the other
# three. `make ship-delta` (in crawler.unturf.com) measures that drift
# and must read 0. arborist holds the reference copy: change it there and
# propagate; never edit a single copy in place.
#
# Heavy crawler dependencies are optional. Each repo declares them under
# its own extra, so install the crawler extra of whichever package this
# copy lives in. Nothing here touches the network at import time, but the
# imports below are real dependencies and this module must not be
# imported unless the caller wants them.
try:
    import aiohttp
    import lxml.html
    from bs4 import BeautifulSoup
    from miniuri import Uri
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "crawler module requires extras: aiohttp, lxml, "
        "beautifulsoup4, miniuri"
    ) from e


class CrawlMode(Enum):
    """Crawl modes for different content types."""
    TEXT = "text"       # Extract text content (default)
    IMAGES = "images"   # Collect images only
    VIDEOS = "videos"   # Collect videos only
    MEDIA = "media"     # All media (images + videos + audio)
    ALL = "all"         # Uber crawl: text + all media


# File extensions by media type
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.ico', '.tiff', '.tif', '.avif'}
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mov', '.avi', '.mkv', '.m4v', '.ogv', '.flv', '.wmv'}
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac', '.wma'}

# Code file extensions
CODE_EXTENSIONS = {
    '.py', '.js', '.ts', '.jsx', '.tsx', '.mjs', '.cjs',  # Python, JavaScript, TypeScript
    '.rs', '.go', '.rb', '.php', '.pl', '.pm',  # Rust, Go, Ruby, PHP, Perl
    '.java', '.kt', '.kts', '.scala', '.groovy',  # JVM languages
    '.c', '.h', '.cpp', '.hpp', '.cc', '.cxx', '.hxx',  # C/C++
    '.cs', '.fs', '.fsx',  # .NET
    '.swift', '.m', '.mm',  # Apple
    '.lua', '.r', '.R', '.jl',  # Lua, R, Julia
    '.sh', '.bash', '.zsh', '.fish', '.ps1',  # Shell
    '.sql', '.graphql', '.gql',  # Query languages
    '.yaml', '.yml', '.toml', '.json', '.xml',  # Config
    '.md', '.rst', '.txt',  # Docs
    '.zig', '.nim', '.d', '.v',  # Modern systems langs
    '.ex', '.exs', '.erl', '.hrl',  # Erlang/Elixir
    '.clj', '.cljs', '.cljc', '.edn',  # Clojure
    '.hs', '.lhs',  # Haskell
    '.ml', '.mli', '.re', '.rei',  # OCaml/ReasonML
    '.lisp', '.cl', '.el', '.scm', '.rkt',  # Lisps
    '.f90', '.f95', '.f03', '.for',  # Fortran
    '.asm', '.s',  # Assembly
    '.cob', '.cbl',  # COBOL
    '.pro',  # Prolog
    '.tcl',  # Tcl
    '.dart',  # Dart
    '.raku', '.p6',  # Raku
    '.cr',  # Crystal
    '.vue', '.svelte',  # Frontend frameworks
    '.tf', '.hcl',  # Terraform
    '.dockerfile', '.makefile',  # Build files
}

# Font file extensions
FONT_EXTENSIONS = {'.woff', '.woff2', '.ttf', '.otf', '.eot', '.sfnt'}

# Style file extensions
STYLE_EXTENSIONS = {'.css', '.scss', '.sass', '.less', '.styl'}

# MIME types by media type
IMAGE_MIME_PREFIXES = ('image/',)
VIDEO_MIME_PREFIXES = ('video/',)
AUDIO_MIME_PREFIXES = ('audio/',)
CODE_MIME_TYPES = {
    'text/x-python', 'application/x-python', 'text/x-python-script',
    'text/javascript', 'application/javascript', 'application/x-javascript',
    'text/typescript', 'application/typescript',
    'text/x-rust', 'text/x-go', 'text/x-ruby', 'application/x-ruby',
    'text/x-java-source', 'text/x-kotlin', 'text/x-scala',
    'text/x-c', 'text/x-c++', 'text/x-csrc', 'text/x-c++src',
    'text/x-csharp', 'text/x-fsharp',
    'text/x-swift', 'text/x-objective-c',
    'text/x-lua', 'text/x-r', 'text/x-julia',
    'text/x-shellscript', 'application/x-sh', 'text/x-bash',
    'application/sql', 'application/graphql',
    'application/json', 'application/xml', 'text/xml',
    'text/yaml', 'application/x-yaml', 'text/x-yaml',
    'text/markdown', 'text/x-markdown',
    'text/plain',  # Often used for code
}
FONT_MIME_TYPES = {
    'font/woff', 'font/woff2', 'font/ttf', 'font/otf', 'font/sfnt',
    'application/font-woff', 'application/font-woff2',
    'application/x-font-ttf', 'application/x-font-otf',
    'application/vnd.ms-fontobject',
}
STYLE_MIME_TYPES = {'text/css', 'text/x-scss', 'text/x-sass', 'text/x-less'}


@dataclass
class MediaItem:
    """Represents a discovered media item."""
    uri: str
    source_page: str
    media_type: str  # 'image', 'video', 'audio'
    mime_type: Optional[str] = None
    md5_hash: Optional[str] = None
    file_size: Optional[int] = None
    alt_text: Optional[str] = None
    title: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    discovered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class MediaMetadata:
    """
    Accumulates metadata from all sources without clobbering.

    "Never clobber, always append" - collects ALL metadata from:
    - img.alt, img.title
    - a.title, a.text (link text)
    - figcaption
    - nearby headings
    - page title/h1

    Produces a combined searchable_text for full-text search.
    """

    def __init__(self):
        self.titles: List[str] = []
        self.alt_texts: List[str] = []
        self.descriptions: List[str] = []
        self.captions: List[str] = []
        self.headings: List[str] = []
        self.link_texts: List[str] = []
        self.link_titles: List[str] = []

    def _add_unique(self, lst: List[str], value: str, max_len: int = 500) -> None:
        """Add value to list if non-empty and not duplicate."""
        if value and value.strip():
            clean = value.strip()[:max_len]
            if clean not in lst:
                lst.append(clean)

    def add_img_alt(self, alt: str) -> None:
        """Add img alt attribute."""
        self._add_unique(self.alt_texts, alt)

    def add_img_title(self, title: str) -> None:
        """Add img title attribute."""
        self._add_unique(self.titles, title)

    def add_link_title(self, title: str) -> None:
        """Add <a title="..."> attribute."""
        self._add_unique(self.link_titles, title)

    def add_link_text(self, text: str) -> None:
        """Add <a> inner text."""
        self._add_unique(self.link_texts, text)

    def add_figcaption(self, caption: str) -> None:
        """Add figcaption text."""
        self._add_unique(self.captions, caption)

    def add_heading(self, heading: str) -> None:
        """Add nearby heading (h1-h6)."""
        self._add_unique(self.headings, heading)

    def add_description(self, desc: str) -> None:
        """Add description (og:description, meta description, etc.)."""
        self._add_unique(self.descriptions, desc)

    def add_page_title(self, title: str) -> None:
        """Add page title."""
        self._add_unique(self.titles, title)

    def get_best_title(self) -> Optional[str]:
        """Get best title for display (first non-empty)."""
        for lst in [self.titles, self.alt_texts, self.link_titles,
                    self.captions, self.link_texts, self.headings]:
            if lst:
                return lst[0]
        return None

    def get_best_alt(self) -> Optional[str]:
        """Get best alt text for accessibility."""
        if self.alt_texts:
            return self.alt_texts[0]
        return self.get_best_title()

    def to_searchable_text(self) -> str:
        """
        Combine ALL collected metadata into searchable text.

        This enables finding images by ANY associated text:
        - "find images of cats" matches img alt="cute cat"
        - "find images from blog post about python" matches page content
        """
        all_parts = []
        # Dedupe while preserving order
        seen = set()
        for lst in [self.titles, self.alt_texts, self.descriptions,
                    self.captions, self.headings, self.link_texts, self.link_titles]:
            for item in lst:
                if item and item not in seen:
                    all_parts.append(item)
                    seen.add(item)
        return ' | '.join(all_parts)

    def to_dict(self) -> Dict[str, Any]:
        """Export all collected metadata as dict."""
        return {
            'titles': self.titles,
            'alt_texts': self.alt_texts,
            'descriptions': self.descriptions,
            'captions': self.captions,
            'headings': self.headings,
            'link_texts': self.link_texts,
            'link_titles': self.link_titles,
            'searchable_text': self.to_searchable_text(),
            'best_title': self.get_best_title(),
            'best_alt': self.get_best_alt(),
        }


# PDF text extraction
try:
    from pypdf import PdfReader
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

logger = logging.getLogger(__name__)

# Default crawl delay
DEFAULT_CRAWL_DELAY = 2.0

# Global last error storage (for error reporting without log parsing)
# Format: {'type': 'robots_txt'|'http_error'|'timeout'|'dns'|'ssl'|'unknown', 'details': str, 'uri': str}
LAST_FETCH_ERROR = None


def get_last_fetch_error() -> Optional[Dict]:
    """Get the last fetch error, if any."""
    return LAST_FETCH_ERROR


def strip_uri_fragment(uri_str: str) -> str:
    """
    Strip the fragment (#anchor) from a URI using miniuri.

    Args:
        uri_str: URI that may contain a fragment

    Returns:
        URI without fragment
    """
    if not uri_str:
        return uri_str

    uri = Uri(uri_str)
    # Reconstruct URI without fragment
    result = f"{uri.scheme}://{uri.authority}{uri.path or ''}"
    if uri.query:
        result += f"?{uri.query}"
    return result


def normalize_link(link) -> Tuple[str, str]:
    """
    Extract URI and anchor text from a link, stripping fragments.

    Args:
        link: Either a dict with 'uri' and optional 'anchor_text', or a string URI

    Returns:
        Tuple of (normalized_uri, anchor_text)
    """
    if isinstance(link, dict):
        uri = strip_uri_fragment(link.get('uri', ''))
        anchor_text = link.get('anchor_text', '')
    else:
        uri = strip_uri_fragment(link)
        anchor_text = ''
    return uri, anchor_text


# Alias for backwards compatibility
strip_uri_fragment = strip_uri_fragment


_META_REFRESH_URI_RE = re.compile(
    r"""^\s*\d+\s*;\s*uri\s*=\s*['"]?([^'"\s>]+)""",
    re.IGNORECASE,
)


def _extract_meta_refresh_uri(base_uri, http_equiv, content):
    """Return absolute URI from a ``<meta http-equiv="refresh" content="…">``
    tag, or None. Case-insensitive on both http-equiv and the ``uri=`` key.
    Handles quoted and bare URIs. Used to recover redirect targets that
    the server delivers as HTML (200 OK) instead of an HTTP 301/302 —
    e.g. Caddy `root` static-page setups (unturf.com root page).
    """
    if not http_equiv or http_equiv.strip().lower() != "refresh":
        return None
    if not content:
        return None
    m = _META_REFRESH_URI_RE.match(content)
    if not m:
        return None
    target = strip_uri_fragment(urljoin(base_uri, m.group(1).strip()))
    parsed = Uri(target)
    if parsed.scheme not in ("http", "https"):
        return None
    return target


def extract_page_links(html, base_uri, extract_anchor_text=False):
    """Extract followable same-page links from HTML for BFS crawling.

    Parses with ``lxml.html`` (a C parser that releases the GIL during
    the parse), so calling this under ``asyncio.to_thread`` across a wide
    fetch wave actually parallelises — unlike BeautifulSoup's pure-Python
    ``html.parser``, which holds the GIL and serialises every "concurrent"
    parse. Falls back to BeautifulSoup if lxml chokes on the markup.

    Honors the same skip rules as before: ``javascript:`` / ``#`` hrefs
    and ``rel="nofollow"`` are dropped; only http(s) links survive;
    fragments are stripped. Also emits ``<meta http-equiv="refresh">``
    target URIs — servers that deliver redirects as HTML (200 OK with
    meta-refresh instead of 301/302) are otherwise invisible to BFS
    (httpx follow_redirects only handles Location headers). Meta-refresh
    targets sort first so the BFS enqueues them before ordinary anchors.

    Returns a list of URI strings, or — when ``extract_anchor_text`` —
    a list of ``{"uri", "anchor_text"}`` dicts (meta-refresh entries
    carry ``anchor_text=""``).
    """
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return _extract_page_links_bs4(html, base_uri, extract_anchor_text)

    result = []
    for meta in doc.iter("meta"):
        target = _extract_meta_refresh_uri(
            base_uri, meta.get("http-equiv"), meta.get("content"),
        )
        if target is None:
            continue
        if extract_anchor_text:
            result.append({"uri": target, "anchor_text": ""})
        else:
            result.append(target)

    for a_tag in doc.iter("a"):
        href = a_tag.get("href")
        if not href or href.startswith(("javascript:", "#")):
            continue
        rel = (a_tag.get("rel") or "").split()
        if "nofollow" in rel:
            continue
        absolute_uri = strip_uri_fragment(urljoin(base_uri, href))
        parsed = Uri(absolute_uri)
        if parsed.scheme in ("http", "https"):
            if extract_anchor_text:
                anchor = (a_tag.text_content() or "").strip()
                result.append({"uri": absolute_uri, "anchor_text": anchor})
            else:
                result.append(absolute_uri)
    return result


def _extract_page_links_bs4(html, base_uri, extract_anchor_text=False):
    """BeautifulSoup fallback for :func:`extract_page_links` — same rules,
    pure-Python parser. Used only when lxml fails to parse the markup."""
    result = []
    soup = BeautifulSoup(html, "html.parser")
    for meta in soup.find_all("meta"):
        target = _extract_meta_refresh_uri(
            base_uri, meta.get("http-equiv"), meta.get("content"),
        )
        if target is None:
            continue
        if extract_anchor_text:
            result.append({"uri": target, "anchor_text": ""})
        else:
            result.append(target)
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if href.startswith(("javascript:", "#")):
            continue
        rel = a_tag.get("rel", [])
        if isinstance(rel, str):
            rel = rel.split()
        if "nofollow" in rel:
            continue
        absolute_uri = strip_uri_fragment(urljoin(base_uri, href))
        parsed = Uri(absolute_uri)
        if parsed.scheme in ("http", "https"):
            if extract_anchor_text:
                anchor_text = a_tag.get_text(strip=True)
                result.append({"uri": absolute_uri, "anchor_text": anchor_text})
            else:
                result.append(absolute_uri)
    return result


def extract_text_from_pdf(pdf_bytes: bytes) -> Optional[str]:
    """
    Extract text from PDF binary data.

    Args:
        pdf_bytes: Raw PDF file bytes

    Returns:
        Extracted text or None if extraction fails
    """
    if not PDF_SUPPORT:
        logger.warning("PDF support not available (pypdf not installed)")
        return None

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text_parts = []
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
        if text_parts:
            return "\n\n".join(text_parts)
        return None
    except Exception as e:
        logger.error(f"Failed to extract text from PDF: {e}")
        return None


def clear_last_fetch_error():
    """Clear the last fetch error."""
    global LAST_FETCH_ERROR
    LAST_FETCH_ERROR = None


async def fetch_robots_txt(uri: str, user_agent: str = "uncloseai.com/1.42") -> Optional[str]:
    """
    Fetch raw robots.txt content for analysis and error reporting.

    Args:
        uri: Any URI on the domain (robots.txt will be fetched from root)
        user_agent: User agent string

    Returns:
        Raw robots.txt content or None if not available
    """
    parsed = Uri(uri)
    robots_uri = f"{parsed.scheme}://{parsed.hostname}/robots.txt"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                robots_uri,
                headers={"User-Agent": user_agent},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status == 200:
                    return await response.text()
    except Exception as e:
        logger.warning(f"Could not fetch robots.txt for error analysis: {e}")

    return None


def analyze_robots_txt_blocking(robots_txt: str, blocked_uri: str, user_agent: str = "uncloseai.com/1.42") -> str:
    """
    Analyze robots.txt to explain why a URI is blocked.

    Args:
        robots_txt: Raw robots.txt content
        blocked_uri: The URI that was blocked
        user_agent: User agent to check against

    Returns:
        Human-readable explanation of the blocking rule
    """
    parsed = Uri(blocked_uri)
    path = parsed.path or "/"

    # Parse robots.txt manually for detailed analysis
    lines = robots_txt.strip().split('\n')
    current_agent = None
    blocking_rule = None
    all_rules = []

    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        if ':' in line:
            key, value = line.split(':', 1)
            key = key.strip().lower()
            value = value.strip()

            if key == 'user-agent':
                current_agent = value
            elif key == 'disallow' and current_agent:
                # Check if this rule applies to us
                agent_matches = (
                    current_agent == '*' or
                    user_agent.lower().startswith(current_agent.lower()) or
                    current_agent.lower() in user_agent.lower()
                )
                if agent_matches:
                    all_rules.append(f"Disallow: {value}")
                    # Check if this rule blocks our path
                    if value and path.startswith(value):
                        blocking_rule = f"Disallow: {value}"

    if blocking_rule:
        result = f"**Blocked by rule:** `{blocking_rule}`\n"
        result += f"**Path requested:** `{path}`\n"
        result += f"**User-Agent:** `{user_agent}`\n\n"

        # Show relevant rules
        if all_rules:
            result += "**Applicable rules:**\n"
            for rule in all_rules[:10]:  # Limit to first 10
                result += f"  • `{rule}`\n"

        return result
    elif all_rules:
        return f"Blocked by robots.txt (exact rule unclear). Rules found:\n" + "\n".join(f"  • `{r}`" for r in all_rules[:10])
    else:
        return "Blocked by robots.txt (no matching disallow rule found - may be a catch-all)"


def get_media_type_from_extension(uri: str) -> Optional[str]:
    """
    Determine media type from URI extension.

    Returns:
        'image', 'video', 'audio', 'code', 'font', 'style', or None
    """
    parsed = Uri(uri)
    if not parsed.path:
        return None
    path = parsed.path.lower()

    # Check for extension match
    for ext in IMAGE_EXTENSIONS:
        if path.endswith(ext):
            return 'image'
    for ext in VIDEO_EXTENSIONS:
        if path.endswith(ext):
            return 'video'
    for ext in AUDIO_EXTENSIONS:
        if path.endswith(ext):
            return 'audio'
    for ext in CODE_EXTENSIONS:
        if path.endswith(ext):
            return 'code'
    for ext in FONT_EXTENSIONS:
        if path.endswith(ext):
            return 'font'
    for ext in STYLE_EXTENSIONS:
        if path.endswith(ext):
            return 'style'
    return None


def get_media_type_from_mime(mime_type: str) -> Optional[str]:
    """
    Determine media type from MIME type.

    Returns:
        'image', 'video', 'audio', 'code', 'font', 'style', or None
    """
    if not mime_type:
        return None

    mime_lower = mime_type.lower()
    # Check prefixes first
    if mime_lower.startswith(IMAGE_MIME_PREFIXES):
        return 'image'
    if mime_lower.startswith(VIDEO_MIME_PREFIXES):
        return 'video'
    if mime_lower.startswith(AUDIO_MIME_PREFIXES):
        return 'audio'
    # Check exact matches for code/font/style
    if mime_lower in CODE_MIME_TYPES:
        return 'code'
    if mime_lower in FONT_MIME_TYPES:
        return 'font'
    if mime_lower in STYLE_MIME_TYPES:
        return 'style'
    return None


@dataclass
class _PageMetadata:
    """Page-level facts attached to every media item found on that page.

    This is what makes a blog image findable by the words of the post it
    illustrates, rather than only by its own alt text.
    """
    title: str = ''
    h1: str = ''
    description: str = ''
    keywords: str = ''
    content: str = ''


def _page_metadata(soup) -> _PageMetadata:
    """Read the page-level metadata once, for reuse by every item."""
    page = _PageMetadata()

    title_tag = soup.find('title')
    if title_tag:
        page.title = title_tag.get_text(strip=True)

    h1_tag = soup.find('h1')
    page.h1 = h1_tag.get_text(strip=True) if h1_tag else ''

    for meta in soup.find_all('meta'):
        name = (meta.get('name') or meta.get('property') or '').lower()
        content = meta.get('content', '')
        if name in ('description', 'og:description'):
            page.description = content[:500]
        elif name == 'keywords':
            page.keywords = content[:500]

    body = soup.find('body')
    if body:
        # Chrome is stripped from a COPY. Decomposing nav/header out of
        # the live tree would silently drop every header logo and nav
        # icon from the extraction that runs against it afterwards.
        body_soup = BeautifulSoup(str(body), 'html.parser')
        for tag in body_soup.find_all(['script', 'style', 'nav',
                                       'footer', 'header', 'aside']):
            tag.decompose()
        page.content = body_soup.get_text(separator=' ', strip=True)[:10000]

    return page


_HEADING_TAGS = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']


def _element_context(element, page: _PageMetadata) -> Dict[str, Any]:
    """Metadata from the elements surrounding one media element."""
    context = {
        'page_title': page.title,
        'page_h1': page.h1,
        'figure_caption': None,
        'nearby_heading': None,
        'link_text': None,
        'link_title': None,      # <a title="..."> attribute
    }

    figure = element.find_parent('figure')
    if figure:
        figcaption = figure.find('figcaption')
        if figcaption:
            context['figure_caption'] = figcaption.get_text(strip=True)[:200]

    link = element.find_parent('a')
    if link:
        link_text = link.get_text(strip=True)
        # Link text identical to the alt is the same information twice.
        if link_text and link_text != element.get('alt', ''):
            context['link_text'] = link_text[:200]
        link_title = link.get('title', '').strip()
        if link_title:
            context['link_title'] = link_title[:200]

    # The nearest preceding heading of ANY level, by document proximity.
    # Looping over tag NAMES in the outer loop instead searched h1 first
    # and only fell through to h2 when the page had no h1 anywhere — so
    # on any page with an h1 this returned that h1, which is already
    # carried separately as page_h1, and the section heading the field
    # exists to capture was lost.
    for prev in element.find_all_previous(_HEADING_TAGS, limit=3):
        heading_text = prev.get_text(strip=True)
        if heading_text:
            context['nearby_heading'] = heading_text[:200]
            break

    return context


class _MediaCollector:
    """Deduping accumulator for the media found on one page."""

    def __init__(self, base_uri: str, page: _PageMetadata):
        self.base_uri = base_uri
        self.page = page
        self.items: List[Dict[str, Any]] = []
        self.seen_uris: Set[str] = set()

    def add(self, uri: str, media_type: str, alt_text: str = None,
            title: str = None, width: int = None, height: int = None,
            element=None, detail_page_uri: str = None) -> None:
        """Record one media item, unless it is already held."""
        if not uri or uri in self.seen_uris:
            return
        if uri.startswith('data:'):
            # Inline bytes are already in hand: nothing to fetch, and
            # the string itself can run to megabytes.
            return

        # Dedup on the RESOLVED address: the same file linked relatively
        # in one place and absolutely in another is one file.
        absolute_uri = urljoin(self.base_uri, uri)
        if absolute_uri in self.seen_uris:
            return
        self.seen_uris.add(absolute_uri)

        if element:
            context = _element_context(element, self.page)
        else:
            context = {'page_title': self.page.title, 'page_h1': self.page.h1}

        metadata = self._accumulate(context, alt_text, title)

        self.items.append({
            'uri': absolute_uri,
            'media_type': media_type,
            'alt_text': metadata.get_best_alt(),
            'title': metadata.get_best_title(),
            'width': width,
            'height': height,
            'source_page': self.base_uri,
            'page_title': self.page.title,
            'page_description': self.page.description,
            'page_keywords': self.page.keywords,
            'page_content': self.page.content,
            'figure_caption': context.get('figure_caption'),
            'nearby_heading': context.get('nearby_heading'),
            'link_text': context.get('link_text'),
            'link_title': context.get('link_title'),
            'detail_page_uri': self._detail_page_for(element, detail_page_uri),
            'searchable_text': metadata.to_searchable_text(),
            'metadata': metadata.to_dict(),
        })

    def _accumulate(self, context: Dict[str, Any], alt_text: str,
                    title: str) -> MediaMetadata:
        """Never clobber, always append — every string near the item."""
        metadata = MediaMetadata()

        if alt_text:
            metadata.add_img_alt(alt_text)
        if title:
            metadata.add_img_title(title)

        if context.get('figure_caption'):
            metadata.add_figcaption(context['figure_caption'])
        if context.get('link_text'):
            metadata.add_link_text(context['link_text'])
        if context.get('link_title'):
            metadata.add_link_title(context['link_title'])
        if context.get('nearby_heading'):
            metadata.add_heading(context['nearby_heading'])
        if context.get('page_title'):
            metadata.add_page_title(context['page_title'])
        if context.get('page_h1'):
            metadata.add_heading(context['page_h1'])

        if self.page.description:
            metadata.add_description(self.page.description)

        return metadata

    def _detail_page_for(self, element, detail_page_uri: str) -> Optional[str]:
        """The page holding the full-res image, for a gallery thumbnail.

        Gallery shape: a thumbnail wrapped in a link to a page that holds
        the full-size image, which resolve_canonical_image then follows.
        """
        if detail_page_uri:
            return urljoin(self.base_uri, detail_page_uri)
        if not element:
            return None

        parent_link = element.find_parent('a', href=True)
        if not parent_link:
            return None

        href = parent_link.get('href', '')
        # A link straight to an image IS the full-res: no page to visit.
        if not href or get_media_type_from_extension(href):
            return None

        resolved = urljoin(self.base_uri, href)
        # Following an offsite link would walk the crawl off the domain
        # it was pointed at.
        if Uri(self.base_uri).hostname != Uri(resolved).hostname:
            return None
        return resolved


def _pixels(value) -> Optional[int]:
    """A dimension attribute as a pixel count.

    None when it isn't one: "100%" and "auto" are CSS, and storing them
    as pixel counts poisons any size filtering downstream.
    """
    return int(value) if value and value.isdigit() else None


def _srcset_uris(srcset: str) -> List[str]:
    """The addresses in a srcset, without their size descriptors."""
    uris = []
    for entry in srcset.split(','):
        entry = entry.strip()
        if entry:
            uris.append(entry.split()[0])
    return uris


# Lazy-loading attributes. On a lazy page there is no real src until
# script runs, so these are the only address a crawler ever sees.
_LAZY_ATTRS = ('data-src', 'data-lazy-src', 'data-original', 'data-srcset')


def _collect_img_tags(soup, add) -> None:
    """<img>: src, every srcset candidate, and the lazy attributes."""
    for img in soup.find_all('img'):
        alt = img.get('alt', '')
        title = img.get('title', '')

        src = img.get('src')
        if src:
            add(src, 'image', alt, title,
                _pixels(img.get('width')), _pixels(img.get('height')),
                element=img)

        # A responsive image is several files; taking only the src
        # misses the resolutions that matter.
        for candidate in _srcset_uris(img.get('srcset', '')):
            add(candidate, 'image', alt, title, element=img)

        for attr in _LAZY_ATTRS:
            lazy = img.get(attr)
            if not lazy:
                continue
            if attr.endswith('srcset'):
                for candidate in _srcset_uris(lazy):
                    add(candidate, 'image', alt, title, element=img)
            else:
                add(lazy, 'image', alt, title, element=img)


def _collect_picture_sources(soup, add) -> None:
    """<picture><source srcset="...">."""
    for picture in soup.find_all('picture'):
        for source in picture.find_all('source'):
            for candidate in _srcset_uris(source.get('srcset', '')):
                add(candidate, 'image')


def _collect_video_tags(soup, add, collect_images: bool) -> None:
    """<video>: src, poster still, and nested <source> elements."""
    for video in soup.find_all('video'):
        title = video.get('title', '')

        src = video.get('src')
        if src:
            add(src, 'video', title=title, element=video)

        # The poster is a still frame — an image asset, not a video.
        poster = video.get('poster')
        if poster and collect_images:
            add(poster, 'image', title=f"Video poster: {title}", element=video)

        for source in video.find_all('source'):
            src = source.get('src')
            if src:
                add(src, 'video', title=title)


def _collect_audio_tags(soup, add) -> None:
    """<audio>: src and nested <source> elements."""
    for audio in soup.find_all('audio'):
        title = audio.get('title', '')

        src = audio.get('src')
        if src:
            add(src, 'audio', title=title)

        for source in audio.find_all('source'):
            src = source.get('src')
            if src:
                add(src, 'audio', title=title)


# Always collected, whatever the mode: these are what a page NEEDS to
# render, not media a mode filter is about.
_ALWAYS_COLLECTED = ('code', 'font', 'style')


def _collect_media_links(soup, add, wanted) -> None:
    """<a href> pointing at a media / code / font / style file."""
    for a in soup.find_all('a', href=True):
        href = a['href']
        media_type = get_media_type_from_extension(href)
        if media_type in wanted or media_type in _ALWAYS_COLLECTED:
            add(href, media_type, alt_text=a.get_text(strip=True)[:100])


def _collect_link_rel(soup, add) -> None:
    """<link>: stylesheets and preloaded fonts/styles."""
    for link in soup.find_all('link', href=True):
        href = link.get('href')
        rel = link.get('rel', [])
        as_attr = link.get('as', '')

        if 'stylesheet' in rel:
            add(href, 'style', alt_text='stylesheet')
        elif 'preload' in rel and as_attr == 'font':
            add(href, 'font', alt_text='preload font')
        elif 'preload' in rel and as_attr == 'style':
            add(href, 'style', alt_text='preload style')
        else:
            media_type = get_media_type_from_extension(href)
            if media_type in ('font', 'style'):
                add(href, media_type, alt_text=f'link {media_type}')


def _collect_scripts(soup, add) -> None:
    """<script src="...">."""
    for script in soup.find_all('script', src=True):
        src = script.get('src')
        if src:
            add(src, 'code', alt_text='script')


# `url(` is CSS syntax, not one of our identifiers — the url->uri
# convention stops at the wire. Spelled `uri(` these patterns matched no
# real stylesheet and the extraction was silently dead.
_FONT_FACE_PATTERN = re.compile(
    r'@font-face\s*\{[^}]*url\(["\']?([^"\')\s]+)["\']?\)',
    re.IGNORECASE | re.DOTALL)
_BACKGROUND_PATTERN = re.compile(
    r'background(?:-image)?\s*:\s*url\(["\']?([^"\')\s]+)["\']?\)',
    re.IGNORECASE)


def _collect_font_faces(soup, add) -> None:
    """@font-face src declarations in <style> blocks."""
    for style_tag in soup.find_all('style'):
        if style_tag.string:
            for match in _FONT_FACE_PATTERN.findall(style_tag.string):
                add(match, 'font', alt_text='font-face')


def _collect_css_backgrounds(soup, add) -> None:
    """CSS background images, inline and in <style> blocks."""
    for elem in soup.find_all(style=True):
        for match in _BACKGROUND_PATTERN.findall(elem.get('style', '')):
            add(match, 'image')

    for style_tag in soup.find_all('style'):
        if style_tag.string:
            for match in _BACKGROUND_PATTERN.findall(style_tag.string):
                add(match, 'image')


# Open Graph / Twitter card property names are spec'd elsewhere; ours to
# read, not ours to rename.
_OG_IMAGE_PROPERTIES = ('og:image', 'og:image:url',
                        'twitter:image', 'twitter:image:src')
_OG_VIDEO_PROPERTIES = ('og:video', 'og:video:url', 'twitter:player')
_SCALAR_META_KEYS = ('og:title', 'og:image:alt', 'media:filename',
                     'media:url', 'media:type')


def _scalar_meta(soup) -> Dict[str, str]:
    """The single-valued meta tags, last occurrence winning."""
    found = {}
    for meta in soup.find_all('meta'):
        key = (meta.get('property', '') or meta.get('name', '')).lower()
        if key in _SCALAR_META_KEYS:
            found[key] = meta.get('content', '')
    return found


def _collect_meta_media(soup, add, collect_images: bool,
                        collect_videos: bool, collect_audio: bool) -> None:
    """Open Graph / Twitter card assets, plus the custom media:url pair."""
    meta = _scalar_meta(soup)
    og_title = meta.get('og:title')
    og_image_alt = meta.get('og:image:alt')
    media_filename = meta.get('media:filename')

    def _content(tag):
        prop = (tag.get('property', '') or tag.get('name', '')).lower()
        return prop, tag.get('content', '')

    if collect_images:
        for tag in soup.find_all('meta'):
            prop, content = _content(tag)
            if content and prop in _OG_IMAGE_PROPERTIES:
                add(content, 'image',
                    alt_text=og_image_alt or og_title or media_filename,
                    title=og_title or media_filename
                    or 'Open Graph/Twitter image')

    if collect_videos:
        for tag in soup.find_all('meta'):
            prop, content = _content(tag)
            if content and prop in _OG_VIDEO_PROPERTIES:
                add(content, 'video',
                    title=og_title or media_filename
                    or 'Open Graph/Twitter video')

    media_uri = meta.get('media:url')
    if not media_uri:
        return

    detected_type = meta.get('media:type') or 'image'    # default to image
    enabled = {'image': collect_images, 'video': collect_videos,
               'audio': collect_audio}
    if enabled.get(detected_type):
        add(media_uri, detected_type, alt_text=og_title,
            title=og_title or media_filename)


def extract_media_from_html(html: str, base_uri: str,
                            mode: CrawlMode = CrawlMode.MEDIA) -> List[Dict[str, Any]]:
    """
    Extract media URIs from HTML content.

    Extracts from:
    - <img src="..."> and <img srcset="...">
    - <picture><source srcset="..."></picture>
    - <video src="..."> and <video poster="...">
    - <source src="..."> (within video/audio)
    - <audio src="...">
    - CSS background-image: url(...)
    - <a href="..."> pointing to media files
    - data-src, data-lazy-src (lazy loading)
    - Open Graph and Twitter card meta tags

    Each source is one _collect_* function taking the soup and the
    collector's ``add``; the order they run in is the order items come
    back in.

    Args:
        html: HTML content
        base_uri: Base URI for resolving relative URIs
        mode: CrawlMode to filter what types to extract

    Returns:
        List of dicts with 'uri', 'media_type', 'alt_text', 'title', etc.
    """
    collect_images = mode in (CrawlMode.IMAGES, CrawlMode.MEDIA, CrawlMode.ALL)
    collect_videos = mode in (CrawlMode.VIDEOS, CrawlMode.MEDIA, CrawlMode.ALL)
    collect_audio = mode in (CrawlMode.MEDIA, CrawlMode.ALL)

    soup = BeautifulSoup(html, "html.parser")
    collector = _MediaCollector(base_uri, _page_metadata(soup))
    add = collector.add

    if collect_images:
        _collect_img_tags(soup, add)
        _collect_picture_sources(soup, add)
    if collect_videos:
        _collect_video_tags(soup, add, collect_images)
    if collect_audio:
        _collect_audio_tags(soup, add)

    wanted = frozenset(kind for kind, on in (('image', collect_images),
                                             ('video', collect_videos),
                                             ('audio', collect_audio)) if on)
    _collect_media_links(soup, add, wanted)
    _collect_link_rel(soup, add)
    _collect_scripts(soup, add)
    _collect_font_faces(soup, add)
    if collect_images:
        _collect_css_backgrounds(soup, add)
    _collect_meta_media(soup, add, collect_images, collect_videos,
                        collect_audio)

    logger.debug(f"Extracted {len(collector.items)} media items from {base_uri}")
    return collector.items


# ============================================================================
# Hydra Mode: Feed/Sitemap Discovery and Parsing
# ============================================================================

@dataclass
class FeedItem:
    """Represents an item discovered from RSS/Atom/Sitemap."""
    uri: str
    title: Optional[str] = None
    published: Optional[str] = None  # ISO date string
    updated: Optional[str] = None
    priority: float = 0.5  # Sitemap priority (0.0-1.0)


def discover_feeds(html: str, base_uri: str) -> List[str]:
    """
    Discover RSS/Atom feed and sitemap URIs from HTML.

    Looks for:
    - <link rel="alternate" type="application/rss+xml">
    - <link rel="alternate" type="application/atom+xml">
    - <link rel="sitemap" href="...">
    - Common sitemap locations: /sitemap.xml, /sitemap_index.xml
    """
    feeds = []
    soup = BeautifulSoup(html, 'html.parser')

    # RSS/Atom feeds from link tags
    for link in soup.find_all('link', rel='alternate'):
        link_type = link.get('type', '')
        href = link.get('href', '')
        if href and ('rss' in link_type or 'atom' in link_type):
            feeds.append(urljoin(base_uri, href))

    # Sitemap from link tag
    for link in soup.find_all('link', rel='sitemap'):
        href = link.get('href', '')
        if href:
            feeds.append(urljoin(base_uri, href))

    # Common sitemap locations
    parsed = Uri(base_uri)
    base = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port and parsed.port not in (80, 443):
        base += f":{parsed.port}"

    common_sitemaps = [
        '/sitemap.xml',
        '/sitemap_index.xml',
        '/sitemap-index.xml',
        '/sitemaps.xml',
    ]
    for path in common_sitemaps:
        sitemap_uri = base + path
        if sitemap_uri not in feeds:
            feeds.append(sitemap_uri)

    return feeds


def parse_rss(xml_content: str, base_uri: str) -> List[FeedItem]:
    """Parse RSS feed and extract items."""
    items = []
    try:
        soup = BeautifulSoup(xml_content, 'xml')
        for item in soup.find_all('item'):
            link = item.find('link')
            title = item.find('title')
            pub_date = item.find('pubDate')

            if link:
                uri = link.get_text().strip() if link.get_text() else link.get('href', '')
                if uri:
                    items.append(FeedItem(
                        uri=urljoin(base_uri, uri),
                        title=title.get_text().strip() if title else None,
                        published=pub_date.get_text().strip() if pub_date else None,
                    ))
    except Exception as e:
        logger.debug(f"RSS parse error: {e}")
    return items


def parse_atom(xml_content: str, base_uri: str) -> List[FeedItem]:
    """Parse Atom feed and extract entries."""
    items = []
    try:
        soup = BeautifulSoup(xml_content, 'xml')
        for entry in soup.find_all('entry'):
            # Atom uses <link href="..."> or <link>uri</link>
            link = entry.find('link')
            title = entry.find('title')
            published = entry.find('published')
            updated = entry.find('updated')

            uri = None
            if link:
                uri = link.get('href') or link.get_text().strip()

            if uri:
                items.append(FeedItem(
                    uri=urljoin(base_uri, uri),
                    title=title.get_text().strip() if title else None,
                    published=published.get_text().strip() if published else None,
                    updated=updated.get_text().strip() if updated else None,
                ))
    except Exception as e:
        logger.debug(f"Atom parse error: {e}")
    return items


def parse_sitemap(xml_content: str, base_uri: str) -> Tuple[List[FeedItem], List[str]]:
    """
    Parse sitemap and extract URIs.

    Returns:
        Tuple of (items, sub_sitemaps) - items are page URIs, sub_sitemaps are nested sitemap URIs
    """
    items = []
    sub_sitemaps = []
    try:
        soup = BeautifulSoup(xml_content, 'xml')

        # Check for sitemap index (contains other sitemaps)
        for sitemap in soup.find_all('sitemap'):
            loc = sitemap.find('loc')
            if loc:
                sub_sitemaps.append(loc.get_text().strip())

        # Regular sitemap URIs.
        #
        # The ELEMENT is <url> — sitemaps.org schema, defined
        # elsewhere. The url->uri convention covers our own identifiers,
        # not wire vocabulary: spelled 'uri' this matched nothing, every
        # valid sitemap parsed to zero page entries and only the
        # <sitemap> index branch worked.
        for uri_tag in soup.find_all('url'):
            loc = uri_tag.find('loc')
            lastmod = uri_tag.find('lastmod')
            priority = uri_tag.find('priority')

            if loc:
                uri = loc.get_text().strip()
                items.append(FeedItem(
                    uri=urljoin(base_uri, uri),
                    updated=lastmod.get_text().strip() if lastmod else None,
                    priority=float(priority.get_text().strip()) if priority else 0.5,
                ))
    except Exception as e:
        logger.debug(f"Sitemap parse error: {e}")
    return items, sub_sitemaps


def parse_feed(content: str, uri: str) -> Tuple[List[FeedItem], List[str]]:
    """
    Auto-detect feed type and parse.

    Returns:
        Tuple of (items, sub_feeds) - sub_feeds for sitemap indexes
    """
    content_lower = content[:1000].lower()

    if '<urlset' in content_lower or '<sitemapindex' in content_lower:
        return parse_sitemap(content, uri)
    elif '<feed' in content_lower:
        return parse_atom(content, uri), []
    elif '<rss' in content_lower or '<channel' in content_lower:
        return parse_rss(content, uri), []
    else:
        logger.debug(f"Unknown feed format: {uri}")
        return [], []


def _detail_page_title(soup) -> Optional[str]:
    """The detail page's own name, by descending specificity.

    Each rung falls through on an EMPTY value, not just a missing tag —
    a present-but-blank og:title is exactly as useless as no og:title.
    """
    title = None

    og_title = soup.find('meta', property='og:title')
    if og_title:
        title = og_title.get('content', '').strip()

    # A one- or two-character alt is layout (a spacer, a bullet), not a name.
    if not title:
        for img in soup.find_all('img', alt=True):
            alt = img.get('alt', '').strip()
            if alt and len(alt) > 2:
                title = alt
                break

    if not title:
        h1_tag = soup.find('h1')
        if h1_tag:
            title = h1_tag.get_text(strip=True)

    if not title:
        title_tag = soup.find('title')
        if title_tag:
            title = title_tag.get_text(strip=True)

    return title


def _detail_page_description(soup) -> Optional[str]:
    """og:description, falling back to the plain description meta."""
    description = None

    og_desc = soup.find('meta', property='og:description')
    if og_desc:
        description = og_desc.get('content', '')

    if not description:
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        if meta_desc:
            description = meta_desc.get('content', '')

    return description


def _detail_page_text(soup, limit: int = 10000) -> Optional[str]:
    """Body text with site chrome removed, for full-text search.

    Nav / header / footer text is identical on every page of a site, so
    leaving it in makes every detail page look alike to an index.

    DESTRUCTIVE: decomposes those elements out of ``soup``. Callers must
    read any metadata they need BEFORE calling this.
    """
    body = soup.find('body')
    if not body:
        return None

    for tag in body.find_all(['script', 'style', 'nav',
                              'footer', 'header', 'aside']):
        tag.decompose()

    return body.get_text(separator=' ', strip=True)[:limit]


def _canonical_by_parent_path(soup, detail_page_uri, thumbnail_uri):
    """REST shape: /media/ID/details displaying /media/ID.

    The detail page is a view OF a resource, so the resource itself is
    the full-res image.
    """
    detail_parsed = Uri(detail_page_uri)
    path_segments = detail_parsed.path.rstrip('/').split('/')
    if len(path_segments) <= 1:
        return None

    parent_path = '/'.join(path_segments[:-1])
    parent_uri = f"{detail_parsed.scheme}://{detail_parsed.hostname}{parent_path}"

    for img in soup.find_all('img', src=True):
        full_src = urljoin(detail_page_uri, img.get('src', ''))
        if full_src == parent_uri or full_src.rstrip('/') == parent_uri:
            if full_src != thumbnail_uri:
                return full_src
    return None


def _canonical_by_versioned_link(soup, detail_page_uri, thumbnail_uri=None):
    """An <a> carrying query params wrapped around an <img>.

    Query params signal a cache-busted or timestamped full-size variant.
    A same-host address with params but no image extension is taken too:
    that is the shape of a render/download endpoint.
    """
    detail_parsed = Uri(detail_page_uri)

    for a_tag in soup.find_all('a', href=True):
        full_href = urljoin(detail_page_uri, a_tag.get('href', ''))
        if '?' not in full_href:
            continue
        if not a_tag.find('img'):
            continue

        if get_media_type_from_extension(full_href) == 'image':
            return full_href

        href_parsed = Uri(full_href)
        if href_parsed.hostname == detail_parsed.hostname or not href_parsed.hostname:
            return full_href
    return None


def _canonical_by_download_link(soup, detail_page_uri, thumbnail_uri=None):
    """<a download> — semantic HTML saying "this is the file"."""
    for a_tag in soup.find_all('a', href=True, download=True):
        href = a_tag.get('href', '')
        if not href:
            continue
        full_href = urljoin(detail_page_uri, href)
        if get_media_type_from_extension(full_href) == 'image':
            return full_href
    return None


def _canonical_by_shared_path(soup, detail_page_uri, thumbnail_uri):
    """Same host, sharing at least two path segments with the page.

    Weakest of the structural rules, so it runs last of the four: shared
    path structure suggests the same underlying resource, but only
    suggests it.
    """
    detail_parsed = Uri(detail_page_uri)
    detail_parts = detail_parsed.path.rstrip('/').split('/')

    for img in soup.find_all('img', src=True):
        full_src = urljoin(detail_page_uri, img.get('src', ''))
        if full_src == thumbnail_uri:
            continue

        img_parsed = Uri(full_src)
        if img_parsed.hostname != detail_parsed.hostname:
            continue

        img_parts = img_parsed.path.rstrip('/').split('/')
        common = sum(1 for d, i in zip(detail_parts, img_parts) if d == i)
        if common >= 2:
            return full_src
    return None


# Structural rules for finding a full-res image, strongest first. Purely
# structural by design — no site-specific strings, so a new gallery needs
# no new code. First rule to return an address wins; og:image is the
# fallback below them all.
_CANONICAL_RULES = (
    _canonical_by_parent_path,
    _canonical_by_versioned_link,
    _canonical_by_download_link,
    _canonical_by_shared_path,
)


def parse_detail_page(html: str, detail_page_uri: str, thumbnail_uri: str,
                      embedding_title: Optional[str] = None) -> Dict[str, Any]:
    """Extract the canonical image and metadata from a detail page.

    Pure: no network, no I/O. It lived as a closure inside
    ``resolve_canonical_image`` purely to capture the four arguments now
    passed explicitly, which put the whole of it — every title rung and
    every canonical rule — behind a socket.

    Title: prefers ``embedding_title`` (from the listing page where the
    image was found, which describes the image) over the detail page's
    own title (which often just names the site).
    """
    soup = BeautifulSoup(html, "html.parser")

    detail_title = _detail_page_title(soup)
    og_image = soup.find('meta', property='og:image')

    result = {
        'detail_page_uri': detail_page_uri,
        'canonical_uri': None,
        'embedding_title': embedding_title,          # from listing page
        'detail_title': detail_title,                # from detail page
        'detail_content': None,                      # body text
        'title': embedding_title or detail_title,    # best available
        'description': _detail_page_description(soup),
        'og_image': og_image.get('content', '') if og_image else None,
    }

    # Body extraction decomposes chrome out of the tree, so it runs after
    # the metadata reads above and before the canonical rules below —
    # the original order, kept deliberately. An <img> inside a <header>
    # stops being a candidate once this has run.
    result['detail_content'] = _detail_page_text(soup)

    canonical_uri = None
    for rule in _CANONICAL_RULES:
        canonical_uri = rule(soup, detail_page_uri, thumbnail_uri)
        if canonical_uri:
            break

    # og:image is the last resort: it is the least specific claim on the
    # page, since a site-wide banner also lives there. Resolved against
    # the page like every rule above — og:image is spec'd absolute but is
    # relative on plenty of real sites, and the caller FETCHES what comes
    # back. Resolve before the thumbnail comparison too, so a relative
    # spelling of the thumbnail cannot slip past it.
    if not canonical_uri and result['og_image']:
        og_uri = urljoin(detail_page_uri, result['og_image'])
        if og_uri != thumbnail_uri:
            canonical_uri = og_uri

    result['canonical_uri'] = canonical_uri
    return result


# A crawl budget of -1 means "no ceiling". Expressed as a large finite
# number so the loop bounds stay ordinary comparisons.
UNLIMITED = 999999

# Addresses that came from a feed or sitemap are known-good content, so
# they enter the link registry pre-scored and get crawled first.
HYDRA_PRIORITY = 100

# What a cross-domain link must score to be worth leaving the domain the
# crawl was pointed at. Deliberately hard to reach.
CROSS_DOMAIN_SCORE_FLOOR = 30

# Modes that collect assets as well as (or instead of) text.
MEDIA_MODES = (CrawlMode.IMAGES, CrawlMode.VIDEOS,
               CrawlMode.MEDIA, CrawlMode.ALL)

_FEED_SUFFIXES = ('.xml', '.rss', '.atom', 'feed', 'feeds')
_FEED_PATH_MARKERS = ('/feed', '/rss', 'atom.xml')


def looks_like_feed(uri: str, html: str) -> bool:
    """Whether a fetch returned a feed where a page was expected.

    Checked by address AND by content, because neither alone is enough:
    plenty of feeds are served from ordinary-looking addresses, and an
    address ending in .rss is a feed whatever it returns. Running text
    extraction over a feed produces noise, and the crawl then follows
    whatever the noise looks like.
    """
    if uri.endswith(_FEED_SUFFIXES):
        return True
    if any(marker in uri for marker in _FEED_PATH_MARKERS):
        return True

    head = html[:500]
    return html.strip().startswith('<?xml') or '<rss' in head or '<feed' in head


def _log_top_candidates(candidates: List[Dict], depth: int) -> None:
    """Log the strongest candidates of each domain class for one wave."""
    same = [c for c in candidates
            if c['is_same_domain'] and c['combined_score'] > 0][:10]
    cross = [c for c in candidates
             if not c['is_same_domain'] and c['combined_score'] > 0][:5]

    if same:
        logger.info(f"Top {len(same)} depth-{depth} same-domain candidates:")
        for i, link in enumerate(same, 1):
            logger.info(f"  {i}. [{link['combined_score']:.1f}] {link['uri']} (keyword={link['link_score']:.1f}, seen={link['seen_count']}x)")

    if cross:
        logger.info(f"Top {len(cross)} depth-{depth} cross-domain candidates (crawled only above {CROSS_DOMAIN_SCORE_FLOOR}):")
        for i, link in enumerate(cross, 1):
            logger.info(f"  {i}. [{link['combined_score']:.1f}] {link['uri']} ({link['domain']}, keyword={link['link_score']:.1f})")


class AsyncWebFetcher:
    """
    Async web fetcher that respects robots.txt and crawl delays.
    Designed for use with Discord bot's async event loop.
    """

    def __init__(
        self,
        user_agent: str = "uncloseai.com/1.42 (ethical web crawler; +https://uncloseai.com)",
        default_crawl_delay: float = DEFAULT_CRAWL_DELAY,
        fast_mode: bool = False
    ):
        self.user_agent = user_agent
        self.default_crawl_delay = default_crawl_delay
        self.fast_mode = fast_mode

        # Timeouts: 5s in fast mode, 60s normally
        self.media_timeout = 5 if fast_mode else 60
        self.page_timeout = 5 if fast_mode else 15

        # Parallel workers for page fetching: 3x CPU in fast mode, 1 otherwise
        import multiprocessing
        self.max_page_workers = multiprocessing.cpu_count() * 3 if fast_mode else 1

        # Caches for robots.txt and crawl delays per domain
        self.robot_parsers: Dict[str, Optional[RobotFileParser]] = {}
        self.domain_crawl_delays: Dict[str, float] = {}
        self.domain_last_fetched: Dict[str, float] = {}

        # One shared ClientSession for the fetcher's lifetime, built
        # lazily inside the event loop on first fetch (see _get_session).
        # Reused across every page so a fast wide crawl keeps connections
        # alive instead of paying TCP+TLS per request — the dominant cost
        # once crawl-delay is 0. Released by aclose().
        self._session: Optional["aiohttp.ClientSession"] = None

        # Page cache: {uri: (html, links, timestamp)}
        self.page_cache: Dict[str, Tuple[str, List, float]] = {}

        # Domain skip list: domains with too many consecutive timeouts
        self.skip_domains: Set[str] = set()
        self.domain_timeout_counts: Dict[str, int] = {}
        self.MAX_CONSECUTIVE_TIMEOUTS = 5

        logger.info(f"AsyncWebFetcher initialized with user-agent: {self.user_agent}")

    async def _get_session(self) -> "aiohttp.ClientSession":
        """Lazily build one shared ClientSession for the fetcher's life.

        A shared session (with a keepalive TCP connector sized to the
        page-worker width) lets a wide fast crawl reuse connections
        across pages instead of opening TCP+TLS per request — measured
        ~3x on a wide wave. Must be built inside a running event loop;
        release it with :meth:`aclose` when the crawl finishes.
        """
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=max(self.max_page_workers * 2, 10),
                limit_per_host=max(self.max_page_workers, 1),
                ttl_dns_cache=300,
                keepalive_timeout=30,
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def aclose(self) -> None:
        """Close the shared session and its pooled connections.

        Safe to call repeatedly and a no-op if no session was built. The
        crawl driver calls this in a ``finally`` so a fast crawl doesn't
        leak sockets (or emit aiohttp's "Unclosed client session"
        warning) on the way out.
        """
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _get_domain(self, uri: str) -> str:
        """Extract domain from URI"""
        parsed = Uri(uri)
        return parsed.hostname

    async def _fetch_robots_txt(self, domain: str) -> Optional[RobotFileParser]:
        """
        Fetch and parse robots.txt for a domain.
        Returns RobotFileParser or None if not available.
        """
        if domain in self.robot_parsers:
            logger.debug(f"Using cached robots.txt for {domain}")
            return self.robot_parsers[domain]

        logger.info(f"Fetching robots.txt for {domain}")
        robots_uri = f"https://{domain}/robots.txt"
        parser = RobotFileParser()
        # ``set_url`` is a stdlib RobotFileParser method — the url→uri
        # rename mistakenly touched it. The instance method name comes
        # from Python's urllib.robotparser and can't be renamed.
        parser.set_url(robots_uri)

        try:
            session = await self._get_session()
            async with session.get(
                robots_uri,
                headers={"User-Agent": self.user_agent},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status == 200:
                    text = await response.text()
                    parser.parse(text.splitlines())
                    self.robot_parsers[domain] = parser

                    # Extract crawl delay (ignored in fast mode)
                    if self.fast_mode:
                        self.domain_crawl_delays[domain] = 0.0
                    else:
                        delay = parser.crawl_delay(self.user_agent)
                        self.domain_crawl_delays[domain] = (
                            delay if delay is not None else self.default_crawl_delay
                        )

                    logger.info(f"Parsed robots.txt for {domain}, crawl delay: {self.domain_crawl_delays[domain]}s")
                    return parser
                else:
                    logger.info(f"No robots.txt for {domain} (status {response.status})")
                    self.robot_parsers[domain] = None
                    self.domain_crawl_delays[domain] = self.default_crawl_delay
                    return None
        except Exception as e:
            logger.warning(f"Could not fetch robots.txt for {domain}: {e}")
            self.robot_parsers[domain] = None
            self.domain_crawl_delays[domain] = self.default_crawl_delay
            return None

    async def _can_fetch(self, uri: str) -> bool:
        """Check if URI can be fetched according to robots.txt"""
        domain = self._get_domain(uri)
        parser = await self._fetch_robots_txt(domain)

        if parser is None:
            # No robots.txt means we can fetch
            return True

        can_fetch = parser.can_fetch(self.user_agent, uri)
        if not can_fetch:
            logger.warning(f"Blocked by robots.txt: {uri}")

        return can_fetch

    async def _enforce_crawl_delay(self, domain: str):
        """Enforce crawl delay for a domain.

        fast_mode's contract is "ignore crawl-delay" — so short-circuit
        here, NOT just on the robots-200 path. The per-domain delay is
        only zeroed in _fetch_robots_txt when robots.txt returns 200;
        on a 404/error the delay falls back to default_crawl_delay (2s),
        which would otherwise make every concurrent fetch wave sleep ~2s
        even under --fast (the dominant cost on a robots-less site).
        """
        if self.fast_mode:
            return
        delay = self.domain_crawl_delays.get(domain, self.default_crawl_delay)
        last_fetched = self.domain_last_fetched.get(domain, 0)
        elapsed = time.time() - last_fetched

        if elapsed < delay:
            sleep_time = delay - elapsed
            logger.info(f"Enforcing crawl delay for {domain}: sleeping {sleep_time:.2f}s")
            await asyncio.sleep(sleep_time)

        self.domain_last_fetched[domain] = time.time()

    async def check_media_uri(
        self,
        uri: str,
        session: Optional[aiohttp.ClientSession] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Check if a URI points to media content using HEAD request.

        Useful for extensionless URIs where we can't determine media type from path.

        Args:
            uri: URI to check
            session: Optional aiohttp session to reuse

        Returns:
            Dict with 'media_type', 'mime_type', 'content_length' if media, None otherwise
        """
        try:
            close_session = session is None
            if session is None:
                session = aiohttp.ClientSession()

            try:
                async with session.head(
                    uri,
                    headers={"User-Agent": self.user_agent},
                    timeout=aiohttp.ClientTimeout(total=10),
                    allow_redirects=True
                ) as response:
                    if response.status != 200:
                        return None

                    content_type = response.headers.get('Content-Type', '').lower()
                    mime_type = content_type.split(';')[0].strip()
                    media_type = get_media_type_from_mime(mime_type)

                    if media_type:
                        content_length = response.headers.get('Content-Length')
                        return {
                            'media_type': media_type,
                            'mime_type': mime_type,
                            'content_length': int(content_length) if content_length else None,
                            'uri': str(response.url)  # Final URI after redirects
                        }
                    return None
            finally:
                if close_session:
                    await session.close()

        except Exception as e:
            logger.debug(f"HEAD check failed for {uri}: {e}")
            return None

    async def resolve_canonical_image(
        self,
        detail_page_uri: str,
        thumbnail_uri: str,
        embedding_title: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch a detail page and extract the canonical (full-res) image URI.

        Universal Algorithm - no hardcoded strings, purely structural detection:

        1. REST pattern: If an <img> src matches detail_page_uri minus last path segment
        2. Wrapped links: <a> tags wrapping <img> where href has query params (versioned)
        3. Download links: <a download> pointing to media
        4. Path similarity: <img> src sharing path structure with detail page
        5. og:image fallback

        Title: Prefers embedding_title (from listing page where image was found).
               Falls back to detail page metadata only if embedding_title not provided.

        Args:
            detail_page_uri: URI of the detail page to fetch
            thumbnail_uri: Original thumbnail URI (to avoid returning same URI)
            embedding_title: Title from the page that linked here (preferred)
            session: Optional aiohttp session to reuse

        Returns:
            Dict with 'canonical_uri', 'title', 'description', 'og_image' or None
        """
        try:
            close_session = session is None
            if session is None:
                session = aiohttp.ClientSession()

            try:
                # Check robots.txt
                if not await self._can_fetch(detail_page_uri):
                    return None

                # Enforce crawl delay
                domain = self._get_domain(detail_page_uri)
                await self._enforce_crawl_delay(domain)

                async with session.get(
                    detail_page_uri,
                    headers={"User-Agent": self.user_agent},
                    timeout=aiohttp.ClientTimeout(total=15),
                    allow_redirects=True
                ) as response:
                    if response.status != 200:
                        return None

                    html = await response.text()

                    # Parse detail page in thread to not block event loop
                    result = await asyncio.to_thread(
                        parse_detail_page, html, detail_page_uri,
                        thumbnail_uri, embedding_title,
                    )
                    logger.info(f"Resolved canonical image from {detail_page_uri}: {result['canonical_uri']}, title: {result['title'][:50] if result['title'] else 'None'}")
                    return result

            finally:
                if close_session:
                    await session.close()

        except Exception as e:
            logger.debug(f"Failed to resolve canonical image from {detail_page_uri}: {e}")
            return None

    def _record_timeout(self, domain: str):
        """Record a timeout for a domain. After MAX_CONSECUTIVE_TIMEOUTS, add to skip list."""
        self.domain_timeout_counts[domain] = self.domain_timeout_counts.get(domain, 0) + 1
        if self.domain_timeout_counts[domain] >= self.MAX_CONSECUTIVE_TIMEOUTS:
            if domain not in self.skip_domains:
                self.skip_domains.add(domain)
                logger.warning(f"Skipping domain {domain} after {self.MAX_CONSECUTIVE_TIMEOUTS} consecutive timeouts")

    def _record_success(self, domain: str):
        """Record a successful fetch, resetting timeout count."""
        self.domain_timeout_counts[domain] = 0

    async def fetch_media(
        self,
        uri: str,
        session: Optional[aiohttp.ClientSession] = None,
        max_size: int = 100 * 1024 * 1024  # 100MB default limit
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch media content and compute MD5 hash.

        Args:
            uri: URI of media to fetch
            session: Optional aiohttp session to reuse
            max_size: Maximum file size in bytes (default 100MB)

        Returns:
            Dict with 'data', 'md5_hash', 'mime_type', 'size', 'uri' or None on failure
        """
        global LAST_FETCH_ERROR

        # Check if domain is in skip list
        domain = self._get_domain(uri)
        if domain in self.skip_domains:
            LAST_FETCH_ERROR = {'type': 'skip_domain', 'details': f'Domain {domain} skipped (too many timeouts)', 'uri': uri}
            logger.debug(f"Skipping {uri}: domain {domain} in skip list")
            return None

        # Check robots.txt
        if not await self._can_fetch(uri):
            LAST_FETCH_ERROR = {'type': 'robots_txt', 'details': 'Blocked by robots.txt', 'uri': uri}
            return None

        # Enforce crawl delay
        await self._enforce_crawl_delay(domain)

        try:
            close_session = session is None
            if session is None:
                session = aiohttp.ClientSession()

            try:
                async with session.get(
                    uri,
                    headers={"User-Agent": self.user_agent},
                    timeout=aiohttp.ClientTimeout(total=self.media_timeout),
                    allow_redirects=True
                ) as response:
                    if response.status != 200:
                        LAST_FETCH_ERROR = {'type': 'http_error', 'details': f'HTTP {response.status}', 'uri': uri}
                        return None

                    # Check content length before downloading
                    content_length = response.headers.get('Content-Length')
                    if content_length and int(content_length) > max_size:
                        LAST_FETCH_ERROR = {'type': 'too_large', 'details': f'File too large: {int(content_length)} bytes', 'uri': uri}
                        logger.warning(f"Skipping {uri}: file too large ({int(content_length)} bytes)")
                        return None

                    content_type = response.headers.get('Content-Type', '').lower()
                    mime_type = content_type.split(';')[0].strip()

                    # Download content
                    data = await response.read()

                    if len(data) > max_size:
                        LAST_FETCH_ERROR = {'type': 'too_large', 'details': f'File too large: {len(data)} bytes', 'uri': uri}
                        return None

                    # Compute MD5 hash
                    md5_hash = hashlib.md5(data).hexdigest()

                    # Determine media type
                    media_type = get_media_type_from_mime(mime_type) or get_media_type_from_extension(uri)

                    # Success - reset timeout count
                    self._record_success(domain)

                    logger.info(f"Fetched media {uri}: {len(data)} bytes, MD5: {md5_hash}, type: {media_type}")

                    return {
                        'data': data,
                        'md5_hash': md5_hash,
                        'mime_type': mime_type,
                        'media_type': media_type,
                        'size': len(data),
                        'uri': str(response.url)  # Final URI after redirects
                    }

            finally:
                if close_session:
                    await session.close()

        except asyncio.TimeoutError:
            self._record_timeout(domain)
            LAST_FETCH_ERROR = {'type': 'timeout', 'details': f'Download timeout ({self.media_timeout}s)', 'uri': uri}
            logger.error(f"Timeout fetching media {uri} ({self.media_timeout}s)")
            return None
        except Exception as e:
            LAST_FETCH_ERROR = {'type': 'unknown', 'details': str(e), 'uri': uri}
            logger.error(f"Error fetching media {uri}: {e}")
            return None

    async def fetch_webpage(
        self,
        uri: str,
        extract_links: bool = False,
        extract_anchor_text: bool = False,
        cache_check_callback = None
    ) -> Tuple[Optional[str], List]:
        """
        Fetch a webpage, respecting robots.txt and crawl delays.
        Uses cache to avoid redundant fetches and delays.

        Args:
            uri: URI to fetch
            extract_links: Whether to extract links from the page
            extract_anchor_text: Whether to extract anchor text with links
            cache_check_callback: Optional async callback to check external cache (e.g., SQLite3)
                                  Should return (html, links) tuple if cached, None otherwise

        Returns:
            If extract_anchor_text=True: Tuple of (html_content, list of dicts with 'uri' and 'anchor_text')
            If extract_anchor_text=False: Tuple of (html_content, list of URIs)
            Returns (None, []) if fetch fails
        """
        global LAST_FETCH_ERROR

        # Validate URI
        if not uri.startswith(("http://", "https://")):
            logger.error(f"Invalid URI scheme: {uri}")
            return None, []

        # Check external cache first (e.g., SQLite3 via WebCacheManager)
        if cache_check_callback:
            try:
                cached_result = await cache_check_callback(uri)
                if cached_result:
                    html, links = cached_result
                    # If we need links but cache has none, re-extract from HTML (in thread to not block)
                    if extract_links and not links and html:
                        logger.info(f"💾 Cache hit but no links cached, re-extracting from HTML: {uri}")
                        links = await asyncio.to_thread(
                            extract_page_links, html, uri, extract_anchor_text
                        )
                        logger.info(f"Re-extracted {len(links)} links from cached HTML")
                    else:
                        logger.info(f"💾 Using SQLite3 cached page: {uri} (skipping robots.txt + crawl delay)")
                    return html, links
            except Exception as e:
                logger.warning(f"Cache check callback failed for {uri}: {e}")

        # Check in-memory cache (fallback for when no external cache available)
        if uri in self.page_cache:
            html, cached_links, cached_time = self.page_cache[uri]
            # If we need links but cache has none, re-extract from HTML (in thread to not block)
            if extract_links and not cached_links and html:
                logger.info(f"💾 In-memory cache hit but no links, re-extracting from HTML: {uri}")
                cached_links = await asyncio.to_thread(
                    extract_page_links, html, uri, extract_anchor_text
                )
                logger.info(f"Re-extracted {len(cached_links)} links from in-memory cached HTML")
            else:
                logger.info(f"💾 Using in-memory cached page: {uri} (cached {time.time() - cached_time:.0f}s ago, skipping crawl delay)")
            return html, cached_links

        # Check robots.txt
        if not await self._can_fetch(uri):
            LAST_FETCH_ERROR = {'type': 'robots_txt', 'details': 'Blocked by robots.txt', 'uri': uri}
            return None, []

        # Enforce crawl delay (only for fresh fetches)
        domain = self._get_domain(uri)
        await self._enforce_crawl_delay(domain)

        # Fetch the page. One GET, no preflight HEAD: aiohttp delivers
        # the response headers before the body is read, so the
        # content-type binary guard below skips images/video/audio
        # without downloading them — the HEAD was a redundant round trip
        # (it doubled per-page latency, the dominant cost of a wide fast
        # crawl). The session is shared (see _get_session) so connections
        # are kept alive and reused across the whole crawl.
        try:
            session = await self._get_session()
            async with session.get(
                uri,
                headers={"User-Agent": self.user_agent},
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
                max_redirects=3
            ) as response:
                if response.status != 200:
                    LAST_FETCH_ERROR = {'type': 'http_error', 'details': f'HTTP {response.status}', 'uri': uri, 'status_code': response.status}
                    logger.warning(f"Failed to fetch {uri}: HTTP {response.status}")
                    return None, []

                # Check content type for binary files (PDF, etc.)
                content_type = response.headers.get('Content-Type', '').lower()
                mime_type = content_type.split(';')[0].strip()

                # Skip images/video/audio before reading the body — these
                # need vision mode, not text fetch.
                if mime_type.startswith(('image/', 'video/', 'audio/')):
                    LAST_FETCH_ERROR = {
                        'type': 'binary_content',
                        'details': f'Content is {mime_type} - requires vision mode for images',
                        'uri': uri,
                        'content_type': mime_type
                    }
                    logger.info(f"Skipping {mime_type} content (not text-extractable): {uri}")
                    return None, []

                is_pdf = 'application/pdf' in content_type or uri.lower().endswith('.pdf')

                if is_pdf:
                    # Handle PDF: read as binary and extract text
                    pdf_bytes = await response.read()
                    logger.info(f"Fetched PDF {uri} ({len(pdf_bytes)} bytes)")
                    pdf_text = extract_text_from_pdf(pdf_bytes)
                    if pdf_text:
                        # Wrap extracted text in minimal HTML for consistent processing
                        # Extract filename from URI for title
                        pdf_filename = uri.split('/')[-1].split('?')[0] or "PDF Document"
                        html = f"<html><head><title>{pdf_filename}</title></head><body><pre>{pdf_text}</pre></body></html>"
                        logger.info(f"Extracted {len(pdf_text)} chars of text from PDF")
                    else:
                        LAST_FETCH_ERROR = {'type': 'pdf_extraction_failed', 'details': 'Could not extract text from PDF', 'uri': uri}
                        logger.warning(f"Failed to extract text from PDF: {uri}")
                        return None, []
                else:
                    html = await response.text()
                logger.info(f"Successfully fetched {uri} ({len(html)} bytes)")

                # Extract links if requested. lxml parsing releases the
                # GIL, so running it in a thread genuinely parallelises
                # across a wide fetch wave (html.parser would not).
                links = []
                if extract_links:
                    links = await asyncio.to_thread(
                        extract_page_links, html, uri, extract_anchor_text
                    )
                    logger.info(f"Extracted {len(links)} links from {uri}")

                # Cache in memory for future fetches in this session
                self.page_cache[uri] = (html, links, time.time())

                return html, links

        except asyncio.TimeoutError:
            LAST_FETCH_ERROR = {'type': 'timeout', 'details': 'Connection timeout (>15 seconds)', 'uri': uri}
            logger.error(f"Timeout fetching {uri}")
            return None, []
        except Exception as e:
            error_str = str(e)
            if 'SSL' in error_str or 'certificate' in error_str.lower():
                LAST_FETCH_ERROR = {'type': 'ssl', 'details': str(e), 'uri': uri}
            elif 'Name or service not known' in error_str or 'DNS' in error_str:
                LAST_FETCH_ERROR = {'type': 'dns', 'details': 'DNS resolution failed', 'uri': uri}
            else:
                LAST_FETCH_ERROR = {'type': 'unknown', 'details': str(e), 'uri': uri}
            logger.error(f"Error fetching {uri}: {e}")
            return None, []

    def extract_text_content(self, html: str) -> str:
        """
        Extract main text content from HTML using BeautifulSoup.

        Args:
            html: HTML content

        Returns:
            Extracted text
        """
        soup = BeautifulSoup(html, "html.parser")

        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.extract()

        # Block-level elements that should create paragraph breaks
        import re
        block_tags = {'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                      'section', 'article', 'header', 'footer', 'main',
                      'li', 'tr', 'blockquote', 'pre', 'br', 'hr'}

        # Insert markers before/after block elements
        for tag in soup.find_all(block_tags):
            tag.insert_before('\n\n')
            tag.insert_after('\n\n')

        # Get text with spaces between inline elements
        text = soup.get_text(separator=' ')

        # Clean up: split on newlines, strip lines, filter empties
        lines = []
        for line in text.split('\n'):
            # Collapse multiple spaces to single space
            cleaned = re.sub(r' +', ' ', line.strip())
            if cleaned:
                lines.append(cleaned)

        # Join with single newlines, then collapse 2+ to paragraph breaks
        text = '\n'.join(lines)
        text = re.sub(r'\n{2,}', '\n\n', text)

        return text

    def extract_title(self, html: str) -> str:
        """Extract title from HTML"""
        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            return soup.title.string.strip()
        return "Untitled"

    def _simple_stem(self, word: str) -> str:
        """
        Simple suffix-stripping stemmer for common English suffixes.

        Args:
            word: Word to stem

        Returns:
            Stemmed word
        """
        word = word.lower()
        # Common suffixes in order of longest first
        suffixes = ['ings', 'ing', 'ed', 'es', 's', 'ly', 'er', 'est', 'tion', 'sion', 'ness', 'ment', 'able', 'ible']
        for suffix in suffixes:
            if word.endswith(suffix) and len(word) > len(suffix) + 2:  # Keep at least 3 chars in stem
                return word[:-len(suffix)]
        return word

    def _score_link(self, link_uri: str, anchor_text: str, query_keywords: Optional[List[str]] = None, keyword_variations: Optional[List[str]] = None) -> float:
        """
        Score a link based on URI and anchor text keyword matches.
        Uses both exact and stemmed matching with different scoring weights.

        Scoring weights:
        - Exact primary keyword match: 10.0 (URI) / 5.0 (anchor)
        - Stemmed primary keyword match: 7.0 (URI) / 3.5 (anchor) [70% of exact]
        - Exact variation match: 6.0 (URI) / 3.0 (anchor) [60% of exact primary]
        - Stemmed variation match: 4.0 (URI) / 2.0 (anchor) [40% of exact primary]

        Args:
            link_uri: URI of the link
            anchor_text: Anchor text of the link
            query_keywords: Primary keywords to match against (full score)
            keyword_variations: Keyword variations/synonyms (reduced score)

        Returns:
            Float score (higher = more relevant to follow)
        """
        if not query_keywords and not keyword_variations:
            return 0.0

        score = 0.0
        uri_lower = link_uri.lower()
        anchor_lower = anchor_text.lower()

        # Score primary keywords (exact and stemmed matching)
        if query_keywords:
            for keyword in query_keywords:
                keyword_lower = keyword.lower()
                keyword_stem = self._simple_stem(keyword_lower)

                # Exact match for keywords in URI path
                if keyword_lower in uri_lower:
                    score += 10.0
                    logger.debug(f"Link URI contains exact primary keyword '{keyword_lower}': {link_uri}")
                # Stemmed match for keywords in URI path (70% of exact)
                elif keyword_stem in uri_lower and keyword_stem != keyword_lower:
                    score += 7.0
                    logger.debug(f"Link URI contains stemmed primary keyword '{keyword_stem}' (from '{keyword_lower}'): {link_uri}")

                # Exact match for keywords in anchor text
                if keyword_lower in anchor_lower:
                    score += 5.0
                    logger.debug(f"Link anchor contains exact primary keyword '{keyword_lower}': {anchor_text}")
                # Stemmed match for keywords in anchor text (70% of exact)
                elif keyword_stem in anchor_lower and keyword_stem != keyword_lower:
                    score += 3.5
                    logger.debug(f"Link anchor contains stemmed primary keyword '{keyword_stem}' (from '{keyword_lower}'): {anchor_text}")

        # Score keyword variations (exact and stemmed matching, reduced weight)
        if keyword_variations:
            for variation in keyword_variations:
                variation_lower = variation.lower()
                variation_stem = self._simple_stem(variation_lower)

                # Exact match for variations in URI path (60% of exact primary)
                if variation_lower in uri_lower:
                    score += 6.0
                    logger.debug(f"Link URI contains exact keyword variation '{variation_lower}': {link_uri}")
                # Stemmed match for variations in URI path (40% of exact primary)
                elif variation_stem in uri_lower and variation_stem != variation_lower:
                    score += 4.0
                    logger.debug(f"Link URI contains stemmed keyword variation '{variation_stem}' (from '{variation_lower}'): {link_uri}")

                # Exact match for variations in anchor text (60% of exact primary)
                if variation_lower in anchor_lower:
                    score += 3.0
                    logger.debug(f"Link anchor contains exact keyword variation '{variation_lower}': {anchor_text}")
                # Stemmed match for variations in anchor text (40% of exact primary)
                elif variation_stem in anchor_lower and variation_stem != variation_lower:
                    score += 2.0
                    logger.debug(f"Link anchor contains stemmed keyword variation '{variation_stem}' (from '{variation_lower}'): {anchor_text}")

        return score

    def _score_page(self, page_data: Dict[str, str], query_keywords: Optional[List[str]] = None) -> float:
        """
        Score a page based on content quality metrics and optional query relevance.
        Higher scores indicate more valuable content.

        Scoring factors:
        - Content length (more content = higher score)
        - Title quality (descriptive titles = higher score)
        - URI quality (cleaner URIs = higher score)
        - Content density (unique words = higher score)
        - Query relevance (if keywords provided, matching content scores higher)

        Args:
            page_data: Dict with 'text', 'title', 'uri' keys
            query_keywords: Optional list of keywords from user's query for relevance scoring

        Returns:
            Float score (0-100+, can exceed 100 with relevance bonus)
        """
        score = 0.0

        # Factor 1: Content length (0-30 points - reduced from 40 to make room for relevance)
        # Award points for substantial content, max at 5000 chars
        text_len = len(page_data.get('text', ''))
        score += min(30, text_len / 167)  # 5000 chars = 30 points

        # Factor 2: Title quality (0-15 points - reduced from 20)
        # Descriptive titles with meaningful length get higher scores
        title = page_data.get('title', '')
        if title and title != "Untitled":
            title_len = len(title)
            if 10 <= title_len <= 100:  # Sweet spot for titles
                score += 15
            elif 5 <= title_len < 10 or 100 < title_len <= 150:
                score += 8
            else:
                score += 3

        # Factor 3: URI quality (0-15 points - reduced from 20)
        # Prefer shorter, cleaner URIs over long query-string heavy URIs
        uri = page_data.get('uri', '')
        if uri:
            # Penalize query strings and fragments
            if '?' in uri:
                score += 3  # Query strings often mean dynamic/less important content
            elif '#' in uri:
                score += 8  # Fragments are slightly better
            else:
                score += 15  # Clean URIs are best

            # Bonus for human-readable paths
            path_parts = uri.split('/')
            if any(len(part) > 3 and part.replace('-', '').replace('_', '').isalnum() for part in path_parts):
                score += 3  # Readable path segments

        # Factor 4: Content density (0-15 points - reduced from 20)
        # Reward pages with substantial, non-repetitive content
        words = []  # Initialize words for use in Factor 5
        if text_len > 0:
            # Check for unique words (simple heuristic for content quality)
            words = page_data.get('text', '').lower().split()
            unique_words = len(set(words))
            if len(words) > 0:
                uniqueness_ratio = unique_words / len(words)
                score += uniqueness_ratio * 15  # Higher uniqueness = better content

        # Factor 5: Query relevance (0-40 points - NEW!)
        # If user's query keywords are provided, boost pages that contain them
        if query_keywords and len(query_keywords) > 0:
            text_lower = page_data.get('text', '').lower()
            title_lower = title.lower()
            uri_lower = uri.lower()

            keyword_matches = 0
            keyword_density = 0.0

            for keyword in query_keywords:
                keyword_lower = keyword.lower()

                # Count occurrences in different sections with different weights
                text_count = text_lower.count(keyword_lower)
                title_count = title_lower.count(keyword_lower)
                uri_count = uri_lower.count(keyword_lower)

                if text_count > 0:
                    keyword_matches += 1
                    keyword_density += text_count

                # Bonus for keywords in title (very relevant)
                if title_count > 0:
                    score += 5 * title_count  # Up to 5 points per title match

                # HIGH BONUS for keywords in URI (strong signal for topic-specific pages)
                # URIs are hand-crafted structure, keywords there mean this page is ABOUT that topic
                if uri_count > 0:
                    score += 15 * uri_count  # Up to 15 points per URI match (5x higher than before!)
                    logger.debug(f"URI contains keyword '{keyword_lower}' ({uri_count}x) - boosting score by {15 * uri_count} points")

            # Award points based on what percentage of query keywords matched
            if len(query_keywords) > 0:
                match_ratio = keyword_matches / len(query_keywords)
                score += match_ratio * 20  # Up to 20 points for matching all keywords

            # Award points for keyword density (how often keywords appear)
            if len(words) > 0 and keyword_density > 0:
                density_score = min(10, (keyword_density / len(words)) * 1000)  # Up to 10 points
                score += density_score

        return score

    async def fetch_feeds(
        self,
        base_uri: str,
        html: str = None,
        max_sitemaps: int = 10,
        feed_uris: List[str] = None,
    ) -> List[FeedItem]:
        """
        Hydra mode: Fetch RSS/Atom/Sitemap feeds.

        Args:
            base_uri: Base URI to discover feeds from
            html: Optional HTML content (if already fetched)
            max_sitemaps: Maximum nested sitemaps to follow
            feed_uris: Optional list of known feed URIs to fetch directly

        Returns:
            List of FeedItems with discovered URIs
        """
        all_items = []
        processed_feeds = set()

        # Use provided feed URIs or discover them
        if feed_uris:
            # Use known feeds directly
            feeds_to_check = list(feed_uris)
            self._last_discovered_feeds = []  # No new discovery
        elif html:
            feeds_to_check = discover_feeds(html, base_uri)
            self._last_discovered_feeds = list(feeds_to_check)  # Track discovered
        else:
            # Try common sitemap locations
            parsed = Uri(base_uri)
            base = f"{parsed.scheme}://{parsed.hostname}"
            if parsed.port and parsed.port not in (80, 443):
                base += f":{parsed.port}"
            feeds_to_check = [
                base + '/sitemap.xml',
                base + '/sitemap_index.xml',
                base + '/feed/',
                base + '/rss/',
                base + '/atom.xml',
            ]
            self._last_discovered_feeds = []  # Will track which ones actually work

        # Queue for processing (includes nested sitemaps)
        to_process = list(feeds_to_check)
        sitemap_count = 0

        while to_process and sitemap_count < max_sitemaps:
            feed_uri = to_process.pop(0)
            if feed_uri in processed_feeds:
                continue
            processed_feeds.add(feed_uri)

            try:
                # Fetch feed content through the fetcher's OWN shared
                # session. This read `self.session`, an attribute no
                # constructor ever set, so every iteration raised
                # AttributeError straight into the except below, was
                # logged at debug, and the method returned [] — feed /
                # sitemap discovery was silently dead for every caller.
                session = await self._get_session()
                async with session.get(
                    feed_uri,
                    headers={'User-Agent': self.user_agent},
                    timeout=aiohttp.ClientTimeout(total=30),
                    allow_redirects=True,
                ) as response:
                    if response.status != 200:
                        logger.debug(f"Feed {feed_uri} returned {response.status}")
                        continue

                    content = await response.text()

                    # Parse feed
                    items, sub_feeds = parse_feed(content, feed_uri)
                    all_items.extend(items)

                    # Add nested sitemaps to queue
                    for sub_feed in sub_feeds:
                        if sub_feed not in processed_feeds:
                            to_process.append(sub_feed)

                    if items:
                        logger.info(f"Hydra: Found {len(items)} URIs in {feed_uri}")
                        # Track working feeds for persistence
                        if not feed_uris and feed_uri not in self._last_discovered_feeds:
                            self._last_discovered_feeds.append(feed_uri)

                    sitemap_count += 1

            except asyncio.TimeoutError:
                logger.debug(f"Timeout fetching feed: {feed_uri}")
            except Exception as e:
                logger.debug(f"Error fetching feed {feed_uri}: {e}")

        logger.info(f"Hydra mode: Discovered {len(all_items)} URIs from {len(processed_feeds)} feeds")
        return all_items

    # ------------------------------------------------------------------
    # Crawl-walk helpers.
    #
    # Each of these ran TWICE inside fetch_with_depth, once for the
    # target page and once per child page, in two copies that had to be
    # kept in step by hand. They are the same operation either way, so
    # they are the same code either way.
    # ------------------------------------------------------------------

    def _register_link(self, link_registry: Dict[str, Dict], link,
                       visited: Set[str], base_domain: str,
                       query_keywords: Optional[List[str]],
                       keyword_variations: Optional[List[str]]) -> Optional[bool]:
        """Record one discovered link, or reinforce one already held.

        A link several pages point at is a stronger signal than one
        seen once, so a repeat sighting ADDS to the score rather than
        replacing it.

        Returns whether the link was same-domain, or None if it was
        already visited and so not registered at all.
        """
        link_uri, anchor_text = normalize_link(link)
        if link_uri in visited:
            return None

        link_domain = self._get_domain(link_uri)
        is_same_domain = (link_domain == base_domain)
        scoring = bool(query_keywords or keyword_variations)

        entry = link_registry.get(link_uri)
        if entry is None:
            link_score = self._score_link(
                link_uri, anchor_text, query_keywords,
                keyword_variations) if scoring else 0.0
            link_registry[link_uri] = {
                'anchor_texts': [anchor_text],
                'seen_count': 1,
                'total_link_score': link_score,
                'domain': link_domain,
                'is_same_domain': is_same_domain,
            }
        else:
            entry['anchor_texts'].append(anchor_text)
            entry['seen_count'] += 1
            if scoring:
                entry['total_link_score'] += self._score_link(
                    link_uri, anchor_text, query_keywords, keyword_variations)

        return is_same_domain

    @staticmethod
    def _scored_candidates(link_registry: Dict[str, Dict],
                           visited: Set[str] = frozenset()) -> List[Dict]:
        """Registry entries as crawl candidates, best first.

        ALL same-domain links sort before ANY cross-domain one: a crawl
        pointed at a domain should exhaust it before wandering off it.
        Within that, each sighting after the first adds two points.
        """
        candidates = []
        for link_uri, meta in link_registry.items():
            if link_uri in visited:
                continue
            seen_count_boost = (meta['seen_count'] - 1) * 2
            candidates.append({
                'uri': link_uri,
                'anchor_texts': meta['anchor_texts'],
                'seen_count': meta['seen_count'],
                'link_score': meta['total_link_score'],
                'combined_score': meta['total_link_score'] + seen_count_boost,
                'domain': meta['domain'],
                'is_same_domain': meta['is_same_domain'],
            })

        candidates.sort(key=lambda x: (not x['is_same_domain'],
                                       -x['combined_score']))
        return candidates

    @staticmethod
    def _eligible_candidates(candidates: List[Dict], visited: Set[str],
                             pages_so_far: int, max_pages: int,
                             query_keywords: Optional[List[str]]) -> List[Dict]:
        """The candidates that clear the thresholds, claimed as visited.

        A same-domain link needs a positive keyword score once keywords
        are in play. A cross-domain link needs CROSS_DOMAIN_SCORE_FLOOR
        on its BOOSTED score — deliberately hard, because following it
        leaves the domain the crawl was pointed at.

        Each returned candidate is added to ``visited`` immediately:
        the batch below fetches in parallel, so a link has to be claimed
        when it is chosen rather than when it comes back.
        """
        eligible: List[Dict] = []
        for link_data in candidates:
            if max_pages > 0 and pages_so_far + len(eligible) >= max_pages:
                break

            if link_data['is_same_domain']:
                if query_keywords and link_data['link_score'] <= 0:
                    continue
            elif link_data['combined_score'] < CROSS_DOMAIN_SCORE_FLOOR:
                continue

            link_uri = link_data['uri']
            if link_uri in visited:
                continue

            eligible.append(link_data)
            visited.add(link_uri)

        return eligible

    async def _build_page(self, uri: str, html: str, links: List,
                          depth: int) -> Dict[str, Any]:
        """One fetched page as a result record.

        Title and text extraction run in a thread: both are pure CPU
        over a parsed tree, and blocking the loop here stalls every
        other fetch in the wave.
        """
        def _extract_content():
            return self.extract_title(html), self.extract_text_content(html)

        title, text = await asyncio.to_thread(_extract_content)
        return {
            "uri": uri,
            "title": title,
            "text": text,
            "html": html,   # raw HTML for cache storage (virtual crawling)
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "depth": depth,
            "links_from_page": [l['uri'] if isinstance(l, dict) else l
                                for l in links],
        }

    @staticmethod
    async def _archive_page(page_callback, uri: str, html: str) -> None:
        """Hand raw HTML to the consumer that asked to archive it.

        A consumer's failure costs that page's archive, never the crawl.
        """
        if not (page_callback and html):
            return
        try:
            await page_callback(uri, html)
        except Exception as e:
            logger.warning(f"Page callback failed for {uri}: {e}")

    @staticmethod
    async def _attach_media(page_data: Dict[str, Any], uri: str, html: str,
                            mode: CrawlMode, media_callback) -> int:
        """Extract media onto a page record and notify the consumer.

        Callbacks are gathered rather than awaited one at a time, and
        exceptions are returned rather than raised: one consumer's
        failure must not hold up or sink the rest.
        """
        if mode not in MEDIA_MODES:
            return 0

        media_items = await asyncio.to_thread(
            extract_media_from_html, html, uri, mode)
        page_data["media"] = media_items

        if media_callback:
            await asyncio.gather(*[media_callback(item)
                                   for item in media_items],
                                 return_exceptions=True)
        return len(media_items)

    async def fetch_with_depth(
        self,
        start_uri: str,
        depth: int = 2,
        max_pages: int = 10,
        query_keywords: Optional[List[str]] = None,
        keyword_variations: Optional[List[str]] = None,
        progress_callback = None,
        cache_check_callback = None,
        should_continue_callback = None,  # Deprecated - kept for compatibility
        user_query: Optional[str] = None,  # Deprecated - kept for compatibility
        mode: CrawlMode = CrawlMode.TEXT,
        media_callback = None,  # Callback for discovered media: async fn(media_item: Dict) -> None
        page_callback = None,  # Callback for page HTML: async fn(uri: str, html: str) -> None
        uris_total_callback = None,  # Callback for URI total updates: fn(total: int) -> None
        initial_visited: Optional[Set[str]] = None,  # Pre-visited URIs for resume support
        hydra_uris: Optional[List[str]] = None,  # Priority URIs from feeds/sitemaps (hydra mode)
    ) -> List[Dict[str, str]]:
        """
        Intelligent keyword-driven crawl strategy with domain prioritization.

        Supports multiple crawl modes:
        - TEXT (default): Extract text content only
        - IMAGES: Collect images from pages
        - VIDEOS: Collect videos from pages
        - MEDIA: Collect all media (images + videos + audio)
        - ALL: Uber crawl - text + all media

        Crawl strategy:
        1. Always fetch target URI first (the URI user asked about) and extract ALL links
        2. Register links in link_registry with seen_count (boost for repeated links)
        3. Sort links: ALL same-domain before ANY cross-domain
        4. Crawl high-scoring links automatically up to max_pages
        5. Cross-domain links only crawled if score >= CROSS_DOMAIN_SCORE_FLOOR

        Example: Searching "dropbox encryption" on https://dropbox.com
        - Target page fetched → extracts links (same-domain and cross-domain)
        - Same-domain links with keyword matches crawled first
        - Cross-domain links only crawled if score is exceptionally high
        - Continues until max_pages or runs out of high-scoring links

        Args:
            start_uri: Starting URI
            depth: How many levels deep to crawl (0 = just this page, 1 = page + links, etc.)
                   Use -1 for unlimited depth (full domain crawl)
            max_pages: Maximum number of pages to fetch (default 10, use -1 for unlimited)
            query_keywords: Optional list of primary keywords from user's query (full scoring weight)
            keyword_variations: Optional list of keyword variations/synonyms (60% scoring weight)
            progress_callback: Optional callback for progress updates
            cache_check_callback: Optional callback for cache checking (not currently used)
            should_continue_callback: DEPRECATED - no longer used
            user_query: DEPRECATED - no longer used
            mode: CrawlMode - what type of content to extract (default: TEXT)
            media_callback: Optional async callback for discovered media items.
                           Called with dict containing 'uri', 'media_type', 'source_page', etc.
                           Use this to process/store media as it's discovered.

        Returns:
            List of dicts with keys: uri, title, text, fetched_at, score, depth, links_from_page
            If mode includes media, each page dict also contains 'media' list
        """
        # -1 means unlimited; a literal -1 would stop before the first link.
        if depth == -1:
            depth = UNLIMITED
        if max_pages == -1:
            max_pages = UNLIMITED

        all_pages: List[Dict[str, Any]] = []
        visited = set(initial_visited) if initial_visited else set()
        base_domain = self._get_domain(start_uri)

        if initial_visited:
            logger.info(f"Resuming with {len(initial_visited)} previously visited pages")

        # {uri: {'anchor_texts', 'seen_count', 'total_link_score',
        #        'domain', 'is_same_domain'}}
        link_registry: Dict[str, Dict] = {}

        # Hydra mode: addresses that came out of a feed or sitemap are
        # known-good content, so they enter pre-scored and go first.
        if hydra_uris:
            logger.info(f"Hydra mode: Injecting {len(hydra_uris)} priority URIs from feeds/sitemaps")
            for uri in hydra_uris:
                if uri not in visited:
                    domain = self._get_domain(uri)
                    link_registry[uri] = {
                        'anchor_texts': ['[hydra]'],
                        'seen_count': HYDRA_PRIORITY,
                        'total_link_score': float(HYDRA_PRIORITY),
                        'domain': domain,
                        'is_same_domain': (domain == base_domain),
                        'from_hydra': True,
                    }

        if query_keywords or keyword_variations:
            logger.info(f"Using query-aware scoring with keywords: {query_keywords}, variations: {keyword_variations}")

        logger.info(f"Starting intelligent crawl with depth={depth}, max_pages={max_pages}, primary_domain={base_domain}")

        # PHASE 1: Always fetch target URI first
        logger.info(f"Phase 1: Fetching target URI: {start_uri}")
        html, links = await self.fetch_webpage(
            start_uri,
            extract_links=True,
            extract_anchor_text=bool(query_keywords),
            cache_check_callback=cache_check_callback,
        )

        if html is None:
            logger.error(f"Failed to fetch target URI {start_uri}")
            return []

        if not html:
            # A cache hit that stored no raw HTML: the page still counts,
            # but there is nothing to extract links from.
            logger.warning(f"Got empty HTML for {start_uri} (likely from cache without raw_html), cannot extract links for virtual crawl")

        if looks_like_feed(start_uri, html):
            logger.warning(f"Skipping RSS/XML feed: {start_uri}")
            return []

        target_page = await self._build_page(start_uri, html, links, 0)
        await self._archive_page(page_callback, start_uri, html)
        media_count = await self._attach_media(target_page, start_uri, html,
                                               mode, media_callback)

        target_page["score"] = self._score_page(target_page,
                                                query_keywords=query_keywords)
        all_pages.append(target_page)
        visited.add(start_uri)

        text = target_page["text"]
        logger.info(f"Crawled target page (depth 0, score {target_page['score']:.1f}): {len(text)} chars, {len(links)} links, {media_count} media extracted")

        if progress_callback:
            await progress_callback(f"📄 **Fetched page 1**\n{target_page['title'][:50]}\n<{start_uri}>\n~{len(text)//4} tokens (estimated)")

        # PHASE 2: Register ALL links from target page
        logger.info(f"Phase 2: Registering {len(links)} links from target page")
        same_domain_count = 0
        cross_domain_count = 0
        for link in links:
            is_same_domain = self._register_link(
                link_registry, link, visited, base_domain,
                query_keywords, keyword_variations)
            if is_same_domain is None:
                continue
            if is_same_domain:
                same_domain_count += 1
            else:
                cross_domain_count += 1

        logger.info(f"Registered {len(link_registry)} unique links from target page (same-domain: {same_domain_count}, cross-domain: {cross_domain_count})")
        if uris_total_callback:
            uris_total_callback(len(link_registry) + 1)  # +1 for target page

        # PHASE 3: Decide whether to crawl deeper
        if depth == 0 or (max_pages > 0 and len(all_pages) >= max_pages):
            logger.info(f"Stopping crawl (depth={depth}, pages={len(all_pages)})")
            return self._finalize_results(all_pages)

        # PHASE 4: Crawl links iteratively, one wave per depth level
        max_depth = depth if depth > 0 else UNLIMITED
        for current_depth in range(1, max_depth + 1):
            if max_pages > 0 and len(all_pages) >= max_pages:
                break

            remaining = "unlimited" if max_pages < 0 else str(max_pages - len(all_pages))
            logger.info(f"Phase 4.{current_depth}: Crawling depth-{current_depth} links (max {remaining} more pages)")

            candidates = self._scored_candidates(link_registry, visited)
            if not candidates:
                logger.info(f"No more unvisited links at depth {current_depth}")
                break

            _log_top_candidates(candidates, current_depth)

            valid_candidates = self._eligible_candidates(
                candidates, visited, len(all_pages), max_pages, query_keywords)
            if not valid_candidates:
                logger.info(f"No valid candidates at depth {current_depth}")
                break

            # Parallel fetching with a semaphore (fast_mode widens it).
            semaphore = asyncio.Semaphore(self.max_page_workers)
            if self.fast_mode and self.max_page_workers > 1:
                logger.info(f"Parallel fetch: {len(valid_candidates)} pages with {self.max_page_workers} workers")

            async def fetch_one(link_data):
                """Fetch a single page under the semaphore."""
                async with semaphore:
                    link_uri = link_data['uri']
                    try:
                        html, child_links = await self.fetch_webpage(
                            link_uri,
                            extract_links=True,
                            extract_anchor_text=bool(query_keywords),
                            cache_check_callback=cache_check_callback,
                        )
                        return {'uri': link_uri, 'html': html,
                                'child_links': child_links,
                                'link_data': link_data, 'error': None}
                    except Exception as e:
                        return {'uri': link_uri, 'html': None,
                                'child_links': [],
                                'link_data': link_data, 'error': str(e)}

            # Process each result as it lands rather than waiting for the
            # whole wave: a slow page must not hold up the pages behind it.
            fetch_tasks = [asyncio.create_task(fetch_one(c))
                           for c in valid_candidates]
            logger.info(f"Streaming {len(fetch_tasks)} fetches (processing as each completes)")

            processed_count = 0
            pages_at_this_depth = 0
            for coro in asyncio.as_completed(fetch_tasks):
                result = await coro
                if max_pages > 0 and len(all_pages) >= max_pages:
                    logger.info(f"Reached max_pages limit ({max_pages}), stopping")
                    break

                link_uri = result['uri']
                html = result['html']
                child_links = result['child_links']
                processed_count += 1

                if not html:
                    reason = f" ({result['error']})" if result['error'] else ""
                    logger.warning(f"Skipping {link_uri} (fetch failed){reason}")
                    continue

                if looks_like_feed(link_uri, html):
                    logger.warning(f"Skipping RSS/XML feed: {link_uri}")
                    continue

                page_data = await self._build_page(link_uri, html,
                                                   child_links, current_depth)
                await self._archive_page(page_callback, link_uri, html)
                media_count = await self._attach_media(
                    page_data, link_uri, html, mode, media_callback)

                page_data["score"] = self._score_page(
                    page_data, query_keywords=query_keywords)
                all_pages.append(page_data)
                pages_at_this_depth += 1

                logger.info(f"Crawled {link_uri} (depth {current_depth}, score {page_data['score']:.1f}): {len(page_data['text'])} chars, {len(child_links)} links, {media_count} media")

                if progress_callback:
                    await progress_callback(f"📄 **Fetched page {len(all_pages)}** (depth {current_depth})\n{page_data['title'][:50]}\n<{link_uri}>")

                # Register newly discovered links for the next wave.
                for link in child_links:
                    self._register_link(link_registry, link, visited,
                                        base_domain, query_keywords,
                                        keyword_variations)

                if uris_total_callback:
                    uris_total_callback(len(link_registry) + 1)

            logger.info(f"Depth {current_depth} complete: processed {processed_count}/{len(fetch_tasks)} results, crawled {pages_at_this_depth} pages")

            # Nothing came back at this depth, so a deeper pass has
            # nothing to work from.
            if pages_at_this_depth == 0:
                logger.info("No pages crawled at this depth - stopping crawl")
                break

        return self._finalize_results(all_pages)

    def _finalize_results(self, all_pages: List[Dict]) -> List[Dict]:
        """Sort and finalize crawl results"""
        # Sort by depth first (main page first), then by score within each depth
        all_pages.sort(key=lambda p: (p.get("depth", 0), -p["score"]))

        logger.info(f"Crawl complete: fetched {len(all_pages)} pages")
        if len(all_pages) > 1:
            logger.info(f"Score range: {all_pages[-1]['score']:.1f} to {all_pages[0]['score']:.1f}")

        return all_pages

    def select_pages_by_token_budget(
        self,
        pages: list,
        token_budget: int,
        reserve_tokens: int = 1000
    ) -> list:
        """
        Select highest-value pages that fit within token budget using greedy knapsack algorithm.

        Args:
            pages: List of page dicts with 'score', 'tokens', 'uri', 'title', 'text' keys
            token_budget: Maximum tokens available for page content
            reserve_tokens: Tokens to reserve for output and system prompts

        Returns:
            List of selected pages (sorted by score descending) that fit in budget
        """
        if not pages:
            return []

        # Adjust budget for reserved tokens
        effective_budget = max(0, token_budget - reserve_tokens)

        if effective_budget <= 0:
            logger.warning(f"Token budget too low after reserving {reserve_tokens} tokens")
            return []

        # Calculate value/token ratio for each page (efficiency metric)
        pages_with_ratio = []
        for page in pages:
            tokens = page.get('tokens', len(page.get('text', '')) // 4)  # Fallback estimate
            score = page.get('score', 0)
            if tokens > 0:
                ratio = score / tokens
                pages_with_ratio.append({
                    **page,
                    'tokens': tokens,
                    'value_per_token': ratio
                })

        if not pages_with_ratio:
            return []

        # Greedy algorithm: Sort by value/token ratio (descending)
        # This maximizes value while minimizing token usage
        pages_with_ratio.sort(key=lambda p: p['value_per_token'], reverse=True)

        # Select pages greedily until budget exhausted
        selected = []
        total_tokens = 0

        for page in pages_with_ratio:
            page_tokens = page['tokens']
            if total_tokens + page_tokens <= effective_budget:
                selected.append(page)
                total_tokens += page_tokens
                logger.debug(f"Selected page: {page['title']} (score={page['score']:.1f}, tokens={page_tokens}, ratio={page['value_per_token']:.3f})")
            else:
                logger.debug(f"Skipped page: {page['title']} (would exceed budget: {total_tokens + page_tokens} > {effective_budget})")

        # Sort selected pages by score (highest first) for presentation
        selected.sort(key=lambda p: p['score'], reverse=True)

        logger.info(f"Selected {len(selected)}/{len(pages)} pages using {total_tokens}/{effective_budget} tokens (budget utilization: {total_tokens/effective_budget*100:.1f}%)")

        return selected


if __name__ == "__main__":
    # Test the async web fetcher
    async def test():
        logging.basicConfig(level=logging.INFO)

        fetcher = AsyncWebFetcher()

        # Test single page fetch
        print("Testing single page fetch...")
        results = await fetcher.fetch_with_depth("https://example.com", depth=0)

        if results:
            print(f"\nFetched: {results[0]['title']}")
            print(f"Content length: {len(results[0]['text'])} chars")
            print(f"Preview: {results[0]['text'][:200]}...")
        else:
            print("Failed to fetch")

    asyncio.run(test())
