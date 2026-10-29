# Tools: what a chat model may call

Until tools, every chat reply was one streamed completion: history in, text
out. With tools a model may stop & ask for something (a web search, a
page), we run it, hand it what came back & it carries on, all inside the
same reply. Everything about that lives in `tools/`; `app.chat_gpt` is its
one caller. If you work on chat & never touch tools, the only lines you
meet are `tool_context=` being passed along & one `while` loop after the
stream.

## Whose network

Our server's network is ours (a home ISP, an egress we answer for), so a
guest's tools never run on it.

| Tool | Runs for a guest or anyone signed in | Runs for an egress user |
|---|---|---|
| `web_search` | **in their own browser**, from their address | on our server |
| `web_fetch` | not offered | on our server |

An egress user is a signed-in email on
`OPENCOMPLETION_SERVER_EGRESS_USERS` (default: nobody). Fetching a page has
no browser path: a browser can't read another site's page (CORS), so
`web_fetch` exists only for egress users.

The browser path: the server sends `tool_request` to the one socket that
sent the message, `static/js/chat/8-tools.js` searches from that browser &
answers `tool_result`. An answer from any other socket is ignored, a
browser that leaves answers at once with an error & one that stays quiet
times out (`OPENCOMPLETION_TOOL_TIMEOUT_S`). Whatever a browser sends back
is cut to shape (`tools.shape_search`) before a model reads it.

Each tool shows as a line inside the reply saying whose network it ran on
("Searched the web from your browser: …", "Read from our server: …") with
the pages it found. Each reply keeps its finished lines as
`Message.tool_trail` (JSON; migration `2026102901`, `make upgrade-db`), so
history shows them & forks copy them.

## Settings (vars.sh)

```bash
export OPENCOMPLETION_WEB_SEARCH=on      # off: no search for anyone
export OPENCOMPLETION_WEB_CRAWL=on       # off: no web_fetch for anyone
export OPENCOMPLETION_SERVER_EGRESS_USERS="fox@unturf.com"   # comma list
export OPENCOMPLETION_TOOL_ROUNDS=4      # asks per reply that may call tools
export OPENCOMPLETION_TOOL_TIMEOUT_S=30  # a browser's time to answer
```

Both tools are on by default. A model or server that refuses tools (a 400)
is asked again without them, so a model that can't call tools still
answers as plain chat. llama.cpp needs `--jinja` to call tools.

## Files

| File | What it holds |
|---|---|
| `tools/__init__.py` | the registry: schemas, `offered` (what runs where), `run`, `shape_search` |
| `tools/policy.py` | every knob above |
| `tools/loop.py` | `ToolCallCollector` (streamed call pieces → calls) & `run_round` |
| `tools/browser.py` | the round trip to the asker's browser |
| `tools/web_fetch.py` | one page, through the shared crawler, public addresses only |
| `tools/keyless_search.py` + `mcp_wire.py` | **vendored** from uncloseai-cli |
| `tools/crawler/async_web_fetcher.py` | **vendored**, shared verbatim with arborist, agents, crawler.unturf.com & neopig |
| `static/js/chat/8-tools.js` | the browser's search & the tool lines |

**Never edit a vendored file here.** `keyless_search.py` differs from
uncloseai-cli's by two lines (its import & `APP_NAME`); the crawler by none
(`make ship-delta` in `~/git/crawler.unturf.com` measures every copy, ours
included). Fix them upstream & copy back. Black skips them
(`pyproject.toml`).

## Keyless search

Five vendors run public anonymous free tiers: Exa, Parallel, Tavily,
Firecrawl & Keenable. No keys, no accounts. Requests round robin from a
random start & fail over to the next vendor. Four of the five answer a
browser (checked 2026-10-29); Keenable needs an `X-Keenable-Title` header
its CORS rules refuse, so it is on the server's ring only.

## web_fetch & our own network

A model chooses the address & reads pages, so a page can talk it into
asking for `http://169.254.169.254/` or our localhost. `PublicOnlyConnector`
dials public addresses only, on every hop, redirects included: it filters
aiohttp's `_resolve_host`, which every connection goes through (a private
method, so `tests/unit/test_tools.py` pins that aiohttp still calls it).
robots.txt & crawl delays are the shared crawler's own. Each fetch runs in
a native thread with its own event loop (`gevent`'s threadpool).

## Tests

- `tests/unit/test_tools.py`: policy, the registry, streamed calls, the
  browser round trip, the address guard (a real redirect to a private
  address included)
- `tests/integration/test_tools_chat.py`: `chat_gpt` with a fake model; a
  guest's search over real Socket.IO test clients
- `tests/functional/test_tools_ui.py` (`make test-ui`): `8-tools.js` in
  headless Chromium, every vendor a stubbed fetch
- `tests/unit/test_keyless_search.py`, `tests/unit/crawler/`: the vendored
  suites, run against our copies

`tests/conftest.py` pins both tools off; the suites above turn them on.

## Adding a tool

A schema in `tools/__init__.py`, a line in `offered` saying where it runs &
for whom, a branch in `run`, & a verb in `8-tools.js`'s `TOOL_VERBS`.
Anything that reaches the network from our server is for egress users only.
