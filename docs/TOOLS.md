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
| `run_code` | on unsandbox, once, **no network** | on unsandbox, once, no network |
| `sandbox_shell`, `sandbox_write_file` | not offered | this chat's own unsandbox sandbox, with network |
| `delegate` (uncloseai-cli) | not offered | in that sandbox |

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

## unsandbox: code runs, a sandbox per chat & uncloseai-cli

Everything here spends our unsandbox account (`UNSANDBOX_PUBLIC_KEY` &
`UNSANDBOX_SECRET_KEY`, the same keys `/api/code` uses) & leaves from
unsandbox's own egress (its DigitalOcean host in California), never ours.

- **`run_code`**: one job, `zerotrust` (no network), Linux or FreeBSD
  (`"os": "freebsd"`: bash, sh, python, perl, lua, c, cpp while jails grow).
  Only files saved under **`/tmp/artifacts/`** come back (unsandbox
  collects nothing else); they become attachments in the chat
  (`blob_store`), so its tool line links them & the model can show an
  image inline. A run that returns none tells the model where files go.
- Every code tool's line folds open to **code & output** (its code or
  command, stdout, stderr, exit code; kept in `tool_trail`, cut to 6k/4k). Guests
  get it unless `OPENCOMPLETION_GUEST_CODE_EXEC=off`.
- **A sandbox per chat** (`sandbox_shell`, `sandbox_write_file`): an
  unsandbox service (`oc-<room>-<random>`, table `sandbox`, migration
  `2026102902`) made on first use from unsandbox's default image,
  `unsandbox-ubuntu` (it carries uncloseai-cli); `OPENCOMPLETION_SANDBOX_IMAGE`
  names another, & the API takes only platform images (`golden-*`) by
  name. `semitrusted` network.
  It keeps files & installs across turns. Idle for
  `OPENCOMPLETION_SANDBOX_IDLE_S` it is **frozen**, never deleted, & the
  next call unfreezes it. A command counts as in use until its time limit,
  so a long one is never frozen under itself.
- **`delegate`**: `unclose -p` in that sandbox, for multi-step work.

### uncloseai-cli's model key

A sandbox runs model-written shell with network, so anything in it can
read its environment, & a page it reads can ask it to send that
somewhere. So it never holds our model keys. It gets:

```
MODEL_ENDPOINT_0 = $OPENCOMPLETION_PUBLIC_URL/api/sandbox/v1
MODEL_API_KEY_0  = ocsb-<random>   # made for this one sandbox
MODEL_ID_0       = the model whose reply delegated
```

in its env vault (`set_service_env`; sourced from
`/run/unsandbox/vault.sh` inside the command, never on a command line).
We keep only the token's SHA-256. `routes/sandbox_proxy.py` is an
OpenAI-compatible `/api/sandbox/v1` (`/models`, `/chat/completions`,
streaming) that answers a **running** sandbox's token with that model
through `get_openai_client_and_model`: our own qwen & hermes keys, server
side. The sandbox can't pick another model or pass anything but chat
fields; each is limited by `OPENCOMPLETION_SANDBOX_PROXY_LIMIT` (600/600).
A leaked token reaches only our proxy, only while its sandbox runs.
`delegate` is offered only with `OPENCOMPLETION_PUBLIC_URL` set, the
address a sandbox reaches us at.

## Settings (vars.sh)

```bash
export OPENCOMPLETION_WEB_SEARCH=on      # off: no search for anyone
export OPENCOMPLETION_WEB_CRAWL=on       # off: no web_fetch for anyone
export OPENCOMPLETION_SERVER_EGRESS_USERS="fox@unturf.com"   # comma list
export OPENCOMPLETION_TOOL_ROUNDS=4      # asks per reply that may call tools
export OPENCOMPLETION_TOOL_TIMEOUT_S=30  # a browser's time to answer
export OPENCOMPLETION_RUN_CODE=on        # off: no run_code for anyone
export OPENCOMPLETION_RUN_CODE_TIMEOUT_S=60
export OPENCOMPLETION_SANDBOX=on         # off: no sandbox tools
export OPENCOMPLETION_SANDBOX_IMAGE=golden-...   # unset: unsandbox-ubuntu
export OPENCOMPLETION_SANDBOX_IDLE_S=600
export OPENCOMPLETION_SANDBOX_TIMEOUT_S=120   # one shell command
export OPENCOMPLETION_DELEGATE=on        # off: no uncloseai-cli
export OPENCOMPLETION_DELEGATE_TIMEOUT_S=900
export OPENCOMPLETION_PUBLIC_URL=https://demo.opencompletion.com
export OPENCOMPLETION_SANDBOX_PROXY_LIMIT=600/600
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
| `tools/sandbox.py` | `run_code`, the sandbox per chat, `delegate` & arming its token |
| `routes/sandbox_proxy.py` | `/api/sandbox/v1`: our model for a sandbox's uncloseai-cli |
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
- `tests/integration/test_sandbox_tools.py`: who gets which unsandbox
  tool, runs & artifacts, one sandbox per chat (made once, woken, frozen
  when idle, never while busy), `delegate`'s token & the proxy (refused
  without a live token, pinned to its model, streams, limited); a fake
  unsandbox, so no test spends the account
- `tests/unit/test_keyless_search.py`, `tests/unit/crawler/`: the vendored
  suites, run against our copies

`tests/conftest.py` pins both tools off; the suites above turn them on.

## Adding a tool

A schema in `tools/__init__.py`, a line in `offered` saying where it runs &
for whom, a branch in `run`, & a verb in `8-tools.js`'s `TOOL_VERBS`.
Anything that reaches the network from our server is for egress users only.
