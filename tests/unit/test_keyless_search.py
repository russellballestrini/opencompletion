# This is free software for the public good of a permacomputer hosted at
# permacomputer.com, an always-on computer by the people, for the people.
# One which is durable, easy to repair, & distributed like tap water
# for machine learning intelligence.
"""Unit tests for tools/keyless_search.py — keyless web search ring.

Copied from uncloseai-cli (tests/test_keyless_search.py), less its
harness-wiring class; our own wiring is tests/unit/test_tools.py.

All offline: transport is mocked, no network. Live verification of the
five vendor endpoints happened 2026-08-20 (all five served keyless).

Absorbed test_keyless_search_coverage.py on 2026-09-02 (ticket 0005).
Its subject is the layer under the vendor parsers: every one of the five
vendors reaches the network through the single `_http_json` primitive,
and `search()` decides whether to fail over or abandon the ring purely
from the TEXT of the error that primitive raises. A status code that
never reaches the message, or a transport exception escaping as
something other than KeylessSearchError, turns a survivable free-tier
throttle into a dead search — for every vendor at once. `_Recorder`
raises on any host it was not primed for, so a regression that widens
the request surface fails loudly instead of spending the operator's
anonymous free tier.
"""

import email.message
import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tools import keyless_search as ks


class _FakeResponse:
    """urlopen's context-manager contract, nothing more."""

    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Recorder:
    """Stands in for urlopen: records the Request it was handed and
    replays a scripted outcome. An unscripted host raises, so no test can
    silently start talking to a real vendor."""

    def __init__(self, outcome=None, by_host=None):
        self.outcome = outcome
        self.by_host = by_host or {}
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append((request, timeout))
        outcome = self.outcome
        if outcome is None:
            for fragment, scripted in self.by_host.items():
                if fragment in request.full_url:
                    outcome = scripted
                    break
        if outcome is None:
            raise AssertionError(
                f'unscripted request escaped the test: {request.full_url}')
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)

    @property
    def request(self):
        return self.calls[-1][0]

    def headers(self, index=-1):
        return {k.lower(): v for k, v in self.calls[index][0].headers.items()}


def _http_error(code, body=b'', reason='scripted'):
    fp = io.BytesIO(body) if body is not None else None
    return urllib.error.HTTPError(
        'https://vendor.example/search', code, reason,
        email.message.Message(), fp)


def _jsonrpc_text(text):
    """A plain-JSON tools/call body carrying one text content item."""
    return json.dumps(
        {'jsonrpc': '2.0', 'id': 1,
         'result': {'content': [{'type': 'text', 'text': text}]}})


EXA_TEXT = (
    'Title: First Result\n'
    'URL: https://one.example\n'
    'Published: N/A\n'
    'Highlights:\n'
    'alpha highlight\n'
    'beta highlight\n'
    '\n---\n'
    'Title: Second Result\n'
    'URL: https://two.example\n'
    'Highlights:\n'
    'gamma\n')


class TestBodyParser(unittest.TestCase):
    """_parse_jsonrpc_body handles JSON, SSE, and error shapes."""

    def test_plain_json_body(self):
        self.assertEqual(
            ks._parse_jsonrpc_body(_jsonrpc_text('hello')), 'hello')

    def test_sse_body(self):
        body = f'event: message\ndata: {_jsonrpc_text("streamed")}\n\n'
        self.assertEqual(ks._parse_jsonrpc_body(body), 'streamed')

    def test_jsonrpc_error_raises(self):
        body = json.dumps({'jsonrpc': '2.0', 'id': 1,
                           'error': {'message': 'bad request'}})
        with self.assertRaises(ks.KeylessSearchError):
            ks._parse_jsonrpc_body(body)

    def test_is_error_tool_result_raises(self):
        body = json.dumps(
            {'jsonrpc': '2.0', 'id': 1,
             'result': {'isError': True,
                        'content': [{'type': 'text',
                                     'text': 'rate limit exceeded'}]}})
        with self.assertRaises(ks.KeylessSearchError) as ctx:
            ks._parse_jsonrpc_body(body)
        self.assertIn('rate limit', str(ctx.exception))

    def test_garbage_body_raises(self):
        with self.assertRaises(ks.KeylessSearchError):
            ks._parse_jsonrpc_body('<html>totally not json</html>')


class TestVendorParsers(unittest.TestCase):
    """Each vendor's payload normalizes to url/title/description/position."""

    def test_exa_formatted_text(self):
        with mock.patch.object(ks, '_http_json',
                               return_value=_jsonrpc_text(EXA_TEXT)):
            results = ks.exa_search('q', limit=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['url'], 'https://one.example')
        self.assertEqual(results[0]['title'], 'First Result')
        self.assertEqual(results[0]['description'],
                         'alpha highlight beta highlight')
        self.assertEqual(results[1]['position'], 2)

    def test_exa_limit(self):
        with mock.patch.object(ks, '_http_json',
                               return_value=_jsonrpc_text(EXA_TEXT)):
            results = ks.exa_search('q', limit=1)
        self.assertEqual(len(results), 1)

    def test_parallel_json_in_text(self):
        payload = json.dumps({'results': [
            {'url': 'https://p.example', 'title': 'P',
             'excerpts': ['one', 'two']}]})
        with mock.patch.object(ks, '_http_json',
                               return_value=_jsonrpc_text(payload)):
            results = ks.parallel_search('q')
        self.assertEqual(results[0]['description'], 'one two')

    def test_tavily_shape(self):
        body = json.dumps({'results': [
            {'url': 'https://t.example', 'title': 'T', 'content': 'snip'}]})
        with mock.patch.object(ks, '_http_json', return_value=body):
            results = ks.tavily_search('q')
        self.assertEqual(results[0]['description'], 'snip')

    def test_firecrawl_shape(self):
        body = json.dumps({'success': True, 'data': {'web': [
            {'url': 'https://f.example', 'title': 'F',
             'description': 'd'}]}})
        with mock.patch.object(ks, '_http_json', return_value=body):
            results = ks.firecrawl_search('q')
        self.assertEqual(results[0]['url'], 'https://f.example')

    def test_keenable_shape(self):
        body = json.dumps({'results': [
            {'url': 'https://k.example', 'title': 'K', 'snippet': 's'}]})
        with mock.patch.object(ks, '_http_json', return_value=body):
            results = ks.keenable_search('q')
        self.assertEqual(results[0]['description'], 's')


class TestRing(unittest.TestCase):
    """Round-robin cursor + pin semantics."""

    def test_pinned_vendor_starts_walk(self):
        order = ks._ring_order('tavily')
        self.assertEqual(order[0], 'tavily')
        self.assertEqual(set(order), set(ks.RING))

    def test_unpinned_rotates(self):
        first = ks._ring_order(None)
        second = ks._ring_order(None)
        self.assertEqual(second[0],
                         ks.RING[(ks.RING.index(first[0]) + 1) % len(ks.RING)])

    def test_unknown_pin_falls_back_to_rotation(self):
        order = ks._ring_order('not-a-vendor')
        self.assertEqual(set(order), set(ks.RING))


class TestFailover(unittest.TestCase):
    """Rate-limit-shaped failures walk the ring; others stop it."""

    def test_rate_limit_fails_over(self):
        calls = []

        def throttled(query, limit=5, timeout=30):
            calls.append('a')
            raise ks.KeylessSearchError('HTTP 429: too many requests')

        def serves(query, limit=5, timeout=30):
            calls.append('b')
            return [{'url': 'https://x', 'title': 'X',
                     'description': '', 'position': 1}]

        order = list(ks.RING)
        searchers = {name: throttled for name in order}
        searchers[order[1]] = serves
        with mock.patch.object(ks, '_SEARCHERS', searchers), \
                mock.patch.object(ks, '_ring_order', return_value=order):
            outcome = ks.search('q')
        self.assertTrue(outcome['success'])
        self.assertEqual(outcome['served_by'], order[1])
        self.assertEqual(calls, ['a', 'b'])

    def test_all_throttled_names_the_ring(self):
        def throttled(query, limit=5, timeout=30):
            raise ks.KeylessSearchError('quota exceeded')

        searchers = {name: throttled for name in ks.RING}
        with mock.patch.object(ks, '_SEARCHERS', searchers), \
                mock.patch.object(ks, '_ring_order',
                                  return_value=list(ks.RING)):
            outcome = ks.search('q')
        self.assertFalse(outcome['success'])
        self.assertIn('all keyless vendors throttled', outcome['error'])

    def test_empty_query_refused(self):
        self.assertFalse(ks.search('')['success'])
        self.assertFalse(ks.search('   ')['success'])

    def test_never_raises_on_payload_drift(self):
        def drifts(query, limit=5, timeout=30):
            raise KeyError('results')

        searchers = {name: drifts for name in ks.RING}
        with mock.patch.object(ks, '_SEARCHERS', searchers), \
                mock.patch.object(ks, '_ring_order',
                                  return_value=list(ks.RING)):
            outcome = ks.search('q')
        self.assertFalse(outcome['success'])
        self.assertIn('unexpected payload', outcome['error'])


class TestRateLimitHeuristic(unittest.TestCase):
    def test_markers(self):
        for msg in ('HTTP 429: nope', 'Rate Limit hit', 'quota exceeded',
                    'please slow down', 'Too Many Requests'):
            self.assertTrue(ks._is_rate_limitish(msg), msg)

    def test_non_markers(self):
        for msg in ('HTTP 400: bad', 'connection refused', '', None):
            self.assertFalse(ks._is_rate_limitish(msg), repr(msg))


class TestHttpJsonRequestShape(unittest.TestCase):
    """The request every vendor rides on. Tavily and Keenable are served
    keyless ONLY because of the identity headers set here, and Exa answers
    as an event stream only because of the Accept header — a silent change
    to either turns a keyless tier into a 401 for all callers."""

    def test_payload_is_posted_as_json_with_content_type(self):
        recorder = _Recorder(outcome=b'{}')
        with mock.patch.object(urllib.request, 'urlopen', recorder):
            ks._http_json('https://vendor.example/search', {'query': 'q'})
        request = recorder.request
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(json.loads(request.data.decode('utf-8')),
                         {'query': 'q'})
        self.assertEqual(recorder.headers()['content-type'],
                         'application/json')

    def test_no_payload_is_a_get_without_a_content_type(self):
        """A bodyless call must not claim a JSON body: some gateways reject
        a Content-Type with no entity, and that failure would look like a
        vendor outage rather than a malformed request."""
        recorder = _Recorder(outcome=b'{}')
        with mock.patch.object(urllib.request, 'urlopen', recorder):
            ks._http_json('https://vendor.example/search')
        self.assertEqual(recorder.request.get_method(), 'GET')
        self.assertIsNone(recorder.request.data)
        self.assertNotIn('content-type', recorder.headers())

    def test_default_identity_and_accept_headers(self):
        recorder = _Recorder(outcome=b'{}')
        with mock.patch.object(urllib.request, 'urlopen', recorder):
            ks._http_json('https://vendor.example/search', {'a': 1})
        headers = recorder.headers()
        self.assertEqual(headers['user-agent'], ks.APP_NAME)
        self.assertIn('text/event-stream', headers['accept'])
        self.assertIn('application/json', headers['accept'])

    def test_vendor_headers_merge_and_may_override_defaults(self):
        """Keenable's app-title header must survive the merge, and a vendor
        must be able to replace the default User-Agent — the merge order is
        the only thing that decides which wins."""
        recorder = _Recorder(outcome=b'{}')
        with mock.patch.object(urllib.request, 'urlopen', recorder):
            ks._http_json('https://vendor.example/search', {'a': 1},
                          headers={'X-Keenable-Title': 'other-app',
                                   'User-Agent': 'other-app'})
        headers = recorder.headers()
        self.assertEqual(headers['x-keenable-title'], 'other-app')
        self.assertEqual(headers['user-agent'], 'other-app')

    def test_timeout_reaches_urlopen(self):
        """An unbounded search call would wedge the agent loop behind a
        vendor that never answers, so the timeout must reach the socket and
        not merely decorate the signature."""
        recorder = _Recorder(outcome=b'{}')
        with mock.patch.object(urllib.request, 'urlopen', recorder):
            ks._http_json('https://vendor.example/search')
            self.assertEqual(recorder.calls[-1][1], ks.DEFAULT_TIMEOUT)
            ks._http_json('https://vendor.example/search', timeout=3)
        self.assertEqual(recorder.calls[-1][1], 3)

    def test_undecodable_body_is_replaced_not_raised(self):
        """A vendor mislabelling its encoding must degrade to mojibake the
        parser can reject, not to a UnicodeDecodeError that escapes the
        KeylessSearchError contract and kills the whole ring walk."""
        recorder = _Recorder(outcome=b'{"t": "\xff\xfe"}')
        with mock.patch.object(urllib.request, 'urlopen', recorder):
            body = ks._http_json('https://vendor.example/search')
        self.assertIn('�', body)


class TestHttpJsonFailureShapes(unittest.TestCase):
    """Every failure must leave as a KeylessSearchError whose message
    carries the evidence `search()` routes on."""

    def test_http_error_carries_status_and_body_head(self):
        recorder = _Recorder(outcome=_http_error(429, b'rate limit exceeded'))
        with mock.patch.object(urllib.request, 'urlopen', recorder):
            with self.assertRaises(ks.KeylessSearchError) as ctx:
                ks._http_json('https://vendor.example/search', {'a': 1})
        message = str(ctx.exception)
        self.assertIn('HTTP 429', message)
        self.assertIn('rate limit exceeded', message)
        # The whole point of preserving both: the ring reads this string.
        self.assertTrue(ks._is_rate_limitish(message))

    def test_error_body_is_clipped_to_a_head(self):
        """An HTML error page can run to megabytes; the exception text ends
        up in a tool result the model reads, so only a head may travel."""
        recorder = _Recorder(outcome=_http_error(500, b'x' * 5000))
        with mock.patch.object(urllib.request, 'urlopen', recorder):
            with self.assertRaises(ks.KeylessSearchError) as ctx:
                ks._http_json('https://vendor.example/search')
        self.assertEqual(str(ctx.exception).count('x'), 300)

    def test_unreadable_error_body_still_reports_the_status(self):
        """A connection that dies while the error body is being read must
        not lose the status code — a 429 read-failure that degraded to a
        generic error would stop the ring instead of failing over."""

        class _Explosive(io.BytesIO):
            def read(self, *args):
                raise OSError('connection reset while reading error body')

        error = urllib.error.HTTPError(
            'https://vendor.example/search', 429, 'Too Many Requests',
            email.message.Message(), _Explosive(b'ignored'))
        recorder = _Recorder(outcome=error)
        with mock.patch.object(urllib.request, 'urlopen', recorder):
            with self.assertRaises(ks.KeylessSearchError) as ctx:
                ks._http_json('https://vendor.example/search')
        self.assertIn('HTTP 429', str(ctx.exception))
        self.assertTrue(ks._is_rate_limitish(str(ctx.exception)))

    def test_url_error_is_wrapped_as_request_failed(self):
        """DNS failure or a refused connection is one vendor being down,
        not the query being wrong — it must arrive as a KeylessSearchError
        so search() classifies it instead of crashing the tool call."""
        recorder = _Recorder(outcome=urllib.error.URLError('name not known'))
        with mock.patch.object(urllib.request, 'urlopen', recorder):
            with self.assertRaises(ks.KeylessSearchError) as ctx:
                ks._http_json('https://vendor.example/search')
        self.assertIn('request failed', str(ctx.exception))
        self.assertIn('name not known', str(ctx.exception))

    def test_socket_timeout_is_wrapped_too(self):
        """A timeout is the most likely free-tier failure and the one most
        likely to escape as a bare exception; it must be catchable by the
        same `except KeylessSearchError` the ring walk uses."""
        recorder = _Recorder(outcome=TimeoutError('timed out'))
        with mock.patch.object(urllib.request, 'urlopen', recorder):
            with self.assertRaises(ks.KeylessSearchError) as ctx:
                ks._http_json('https://vendor.example/search', timeout=1)
        self.assertIn('request failed', str(ctx.exception))
        # A bare timeout does NOT read as throttling, so the ring stops.
        self.assertFalse(ks._is_rate_limitish(str(ctx.exception)))


class TestTransportDrivesRingFailover(unittest.TestCase):
    """End-to-end through the real transport: only the socket is faked, so
    this covers the seam where an HTTP status becomes a routing decision."""

    def test_throttled_vendor_hands_off_to_the_next_in_the_ring(self):
        tavily_body = json.dumps({'results': [
            {'url': 'https://served.example', 'title': 'S',
             'content': 'body'}]}).encode('utf-8')
        recorder = _Recorder(by_host={
            'mcp.exa.ai': _http_error(429, b'{"error":"rate limit"}'),
            'search.parallel.ai': _http_error(429, b'too many requests'),
            'api.tavily.com': tavily_body,
        })
        with mock.patch.object(urllib.request, 'urlopen', recorder):
            outcome = ks.search('permacomputer', vendor='exa')
        self.assertTrue(outcome['success'], outcome)
        self.assertEqual(outcome['served_by'], 'tavily')
        self.assertEqual(outcome['results'][0]['url'], 'https://served.example')
        # Ring order was walked in sequence and stopped at the first vendor
        # that served: a failover, not a broadcast to all five.
        hosts = [call[0].full_url for call in recorder.calls]
        self.assertEqual(len(hosts), 3)
        self.assertIn('mcp.exa.ai', hosts[0])
        self.assertIn('search.parallel.ai', hosts[1])
        self.assertIn('api.tavily.com', hosts[2])

    def test_non_throttle_status_stops_the_walk_at_one_vendor(self):
        """A 400 means the query is wrong; replaying it against four more
        vendors burns four more free-tier calls to earn the same answer."""
        recorder = _Recorder(outcome=_http_error(400, b'malformed query'))
        with mock.patch.object(urllib.request, 'urlopen', recorder):
            outcome = ks.search('q', vendor='firecrawl')
        self.assertFalse(outcome['success'])
        self.assertEqual(outcome['served_by'], 'firecrawl')
        self.assertIn('HTTP 400', outcome['error'])
        self.assertIn('malformed query', outcome['error'])
        self.assertEqual(len(recorder.calls), 1)


class TestBodyParserFallbacks(unittest.TestCase):
    """The JSON-RPC reply shapes that are neither a clean JSON body nor a
    clean SSE stream — Exa and Parallel both drift here."""

    def test_non_json_sse_lines_are_skipped(self):
        """A stream that opens with a keepalive or a `[DONE]` sentinel must
        not abort the scan before the real payload line arrives."""
        body = ('data: [DONE-ish sentinel]\n'
                'data: not json at all\n'
                f'data: {json.dumps({"result": {"content": [{"text": "found"}]}})}\n')
        self.assertEqual(ks._parse_jsonrpc_body(body), 'found')

    def test_truncated_json_body_falls_through_to_the_sse_scan(self):
        """A body that starts with `{` but is cut short must not swallow a
        well-formed `data:` line further down — half a JSON object is the
        classic shape of a proxied stream."""
        body = ('{"jsonrpc": "2.0", "resu\n'
                f'data: {json.dumps({"result": {"content": [{"text": "late"}]}})}\n')
        self.assertEqual(ks._parse_jsonrpc_body(body), 'late')

    def test_malformed_json_event_lines_are_skipped(self):
        """A stream chunked mid-object emits a `data:` line that opens like
        JSON and is not; abandoning the scan there would discard the intact
        payload event that follows and fail a search that did succeed."""
        body = ('data: {"result": {"content": [{"text": "trunca\n'
                f'data: {json.dumps({"result": {"content": [{"text": "whole"}]}})}\n')
        self.assertEqual(ks._parse_jsonrpc_body(body), 'whole')

    def test_stream_of_only_malformed_events_is_an_error(self):
        body = 'data: {"result": broken\ndata: {"still": broken\n'
        with self.assertRaises(ks.KeylessSearchError) as ctx:
            ks._parse_jsonrpc_body(body)
        self.assertIn('unrecognized', str(ctx.exception))

    def test_content_without_any_text_item_is_unrecognized(self):
        """An image-only or empty content array carries no search results;
        reporting it as an error lets the ring try another vendor instead
        of returning a confidently empty answer."""
        body = json.dumps({'result': {'content': [
            {'type': 'image', 'data': 'x'}, {'type': 'text', 'text': ''}]}})
        with self.assertRaises(ks.KeylessSearchError) as ctx:
            ks._parse_jsonrpc_body(body)
        self.assertIn('unrecognized', str(ctx.exception))

    def test_error_object_without_message_still_raises_with_detail(self):
        body = json.dumps({'error': {'code': -32000}})
        with self.assertRaises(ks.KeylessSearchError) as ctx:
            ks._parse_jsonrpc_body(body)
        self.assertIn('-32000', str(ctx.exception))

    def test_is_error_without_text_gets_a_fallback_message(self):
        body = json.dumps({'result': {'isError': True, 'content': []}})
        with self.assertRaises(ks.KeylessSearchError) as ctx:
            ks._parse_jsonrpc_body(body)
        self.assertIn('tool call failed', str(ctx.exception))


class TestVendorLimits(unittest.TestCase):
    """`limit` is the operator's token budget for the search result block;
    a vendor over-serving it must be trimmed on our side, not theirs."""

    def _rows(self, count, key='url'):
        return [{key: f'https://r{i}.example', 'title': f'T{i}'}
                for i in range(count)]

    def test_parallel_truncates_an_over_serving_vendor(self):
        payload = json.dumps({'results': self._rows(6)})
        text = json.dumps({'result': {'content': [{'text': payload}]}})
        with mock.patch.object(ks, '_http_json', return_value=text):
            results = ks.parallel_search('q', limit=2)
        self.assertEqual([r['position'] for r in results], [1, 2])

    def test_firecrawl_truncates_an_over_serving_vendor(self):
        body = json.dumps({'data': {'web': self._rows(6)}})
        with mock.patch.object(ks, '_http_json', return_value=body):
            results = ks.firecrawl_search('q', limit=3)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[-1]['url'], 'https://r2.example')

    def test_keenable_truncates_and_prefers_snippet_over_description(self):
        rows = self._rows(4)
        rows[0]['snippet'] = 'the snippet'
        rows[0]['description'] = 'the description'
        rows[1]['description'] = 'only a description'
        body = json.dumps({'results': rows})
        with mock.patch.object(ks, '_http_json', return_value=body):
            results = ks.keenable_search('q', limit=2)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['description'], 'the snippet')
        self.assertEqual(results[1]['description'], 'only a description')

    def test_firecrawl_tolerates_a_missing_data_envelope(self):
        """Firecrawl answers `{"success": false}` with no `data` key when it
        throttles; that must read as zero results, not an AttributeError
        that `search()` reports as unexpected payload drift."""
        with mock.patch.object(ks, '_http_json',
                               return_value=json.dumps({'success': False})):
            self.assertEqual(ks.firecrawl_search('q'), [])


class TestStandaloneEntryPoint(unittest.TestCase):
    """The module advertises itself as drop-in transferable, and its own
    `python keyless_search.py <query>` entry is how anyone checks that
    claim before wiring it into a project."""

    def test_script_entry_prints_a_json_result_without_a_live_call(self):
        import io as _io
        import runpy
        from contextlib import redirect_stdout

        module_path = os.path.join(ROOT, 'tools', 'keyless_search.py')
        refuse = _Recorder(outcome=urllib.error.URLError('offline in tests'))
        buffer = _io.StringIO()
        with mock.patch.object(urllib.request, 'urlopen', refuse), \
                mock.patch.object(sys, 'argv',
                                  ['keyless_search.py', 'permacomputer']), \
                redirect_stdout(buffer):
            runpy.run_path(module_path, run_name='__main__')
        printed = json.loads(buffer.getvalue())
        self.assertFalse(printed['success'])
        self.assertIn('request failed', printed['error'])


if __name__ == '__main__':
    unittest.main()
