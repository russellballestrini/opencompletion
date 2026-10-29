"""Binds our copy of the shared crawler to the name its suites import.

tools/crawler/async_web_fetcher.py & the six suites beside this file are
vendored VERBATIM from the crawler shared by arborist,
agents.ai.unturf.com, crawler.unturf.com & neopig (`make ship-delta` in
crawler.unturf.com fails on any drift). The import path is the one thing
that cannot be shared, so it lives here, bound to ``shared_web_fetcher``;
without the crawler's dependencies the name never appears & the suites
skip themselves.
"""

import sys as _sys

try:
    from tools.crawler import async_web_fetcher as _fetcher
except ImportError:  # crawler dependencies not installed
    pass
else:
    _sys.modules.setdefault("shared_web_fetcher", _fetcher)
