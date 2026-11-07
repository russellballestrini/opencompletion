# This is free software for the public good of a permacomputer hosted at
# permacomputer.com, an always-on computer by the people, for the people.
# One which is durable, easy to repair, & distributed like tap water
# for machine learning intelligence.
#
# A permacomputer is community-owned infrastructure optimized around
# four values:
#
#   TRUTH      First principles, math & science, open source code freely distributed
#   FREEDOM    Voluntary partnerships, freedom from tyranny & corporate control
#   HARMONY    Minimal waste, self-renewing systems with diverse thriving connections
#   LOVE       Be yourself without hurting others, cooperation through natural law
#
# This software contributes to that vision by enabling code execution across 42+ programming languages through a unified interface, accessible to all.
# Code is seeds to sprout on any abandoned technology.

"""Repair of malformed tool-call arguments: the model-agnostic half of
uncloseai-cli's parse/purify layer (tool_calls.py), stdlib only.

A model's tool call arrives as a name & an `arguments` string that should be
a JSON object. Models break it: raw newlines or control characters inside a
file body, invalid escapes, lone surrogates, trailing commas, an object cut
off mid-value, a markdown fence around it, the real arguments wrapped in a
second `arguments`/`input`/`params`, `{"key":K,"value":V}` pairs, parameter
tags of an XML dialect leaking into a value, a dialect token pasted on the
end. This module turns those into the object the model meant, and changes
the model never.

Zero coupling, both ways: it imports only the standard library, and nothing
here knows any tool set. A caller passes what is tool-specific (key
aliases, the field a bare value belongs to). uncloseai-cli's tool_calls.py
imports these functions back under their old names; opencompletion vendors
this file verbatim (never edit it there).

Telemetry is a hook: `set_recorder(fn)` receives each repair as
`fn(switch, action, **ctx)`, the shape of uncloseai-cli's
telemetry.record_switch, which is where the measured save rates come from
(scripts/switch-metrics.py). `repair_arguments` also returns the repairs
it made, so a caller can count its own.
"""

import json
import re
from contextvars import ContextVar

_recorder = None
_noted = ContextVar("toolcall_repair_noted", default=None)


def set_recorder(fn):
    """Receive every repair as fn(switch, action, **ctx); None turns it off."""
    global _recorder
    _recorder = fn


def _record(switch, action, **ctx):
    noted = _noted.get()
    if noted is not None:
        noted.append(switch)
    if _recorder is not None:
        _recorder(switch, action, **ctx)


_NESTED_ARGUMENT_KEYS = ('arguments', 'args', 'input', 'parameters', 'params')


def _loads_envelope_inner(text):
    """The object inside an arguments envelope, or None.

    Plain JSON first. Failing that, ONE object followed by nothing but
    stray closing brackets: qwen3.8 on forward-deployed attempt 27
    (2026-09-23) closed the envelope's brace inside the string -- '
    {"command": "ls -la ..."}}' -- twice in one at-bat; the second
    ended it as an empty turn and the proposal it carried never
    landed. Anything else after the object is left unparsed."""
    for candidate in (text, _double_invalid_escapes(text)):
        try:
            return json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            pass
    s = _double_invalid_escapes(text.strip())
    if not s.startswith('{'):
        return None
    try:
        obj, end = json.JSONDecoder().raw_decode(s)
    except (ValueError, json.JSONDecodeError):
        return _single_field_object(text)
    if _LEAK_CLOSE_TAG_RE.sub('', s[end:]).strip(' \t\r\n}]"'):
        return None
    return obj


_SINGLE_FIELD_RE = re.compile(
    r'^\s*\{\s*"(\w+)"\s*:\s*"(.*)"\s*\}+\s*(?:</+[\w:-]*>?\s*)*$', re.S)


def _single_field_object(text):
    """A one-field object whose value carries raw, unescaped quotes:
    `{"command":"grep -rn "a\\|b" ."}}</tool_call>` (space-bunny-alpha,
    catan, 2026-09-30). With ONE field the value is unambiguous -- the
    first `":"` to the last `"}` -- so it is taken whole; escaped quotes
    in it are unescaped. Several fields with raw quotes stay unparsed."""
    m = _SINGLE_FIELD_RE.match(text)
    if not m or '", "' in m.group(2) and re.search(r'",\s*"\w+"\s*:', m.group(2)):
        return None
    return {m.group(1): m.group(2).replace('\\"', '"')}


# A closing tag of the model's OWN call dialect, left behind when its
# XML envelope was spliced into JSON: `</arguments>`, `</</arguments>`,
# `</command>`, `</tool_call>`, and MiniMax's `]<]minimax[` (2026-09-29).
_LEAK_CLOSE_TAG_RE = re.compile(r'</+[\w:-]*>?|\]<\][\w:-]*\[?')


# A trailing run of those tags, with the quote/brace debris around them.
# Anchored on a TAG: a value that merely ends in `"` or `}` is content.
_LEAK_TAIL_RE = re.compile(
    r'(?:[\s"}\]\[<>]*(?:</+[\w:-]*>?|\]<\][\w:-]*\[?))+[\s"}\]\[<>]*$')


# `command": "cd x && ...` -- a field spelled into a KEY.
# The separator is `:` in JSON and `=` where the XML attribute spelling
# leaked in: `command="cd /w && git add -A ...` (2026-09-30).
_LEAK_FIELD_RE = re.compile(r'^\s*"?(\w+)"?\s*[:=]\s*"?(.*)$', re.DOTALL)


# `arguments": {...` / `arguments">{...` -- an envelope spelled into a key.
_LEAK_ENVELOPE_RE = re.compile(
    r'^\s*"?(?:' + '|'.join(_NESTED_ARGUMENT_KEYS) + r')"*\s*[:>]?\s*',
    re.DOTALL)


# One field of an object that will not parse, up to the next field.
_LEAK_OBJ_FIELD_RE = re.compile(
    r'"(\w+)"\s*:\s*"(.*?)'
    r'(?="\s*,\s*"\w+"\s*:|</\w+>?\s*"?\w+"\s*:|\Z)', re.DOTALL)


def _leak_body(text):
    """A raw body after a header object: fences and tags peeled off."""
    body = _LEAK_TAIL_RE.sub('', text).strip('\n')
    m = re.match(r'^\s*```[\w.+-]*\n(.*?)(?:\n```\s*)?$', body, re.DOTALL)
    if m:
        body = m.group(1)
    return body.strip('\n') + '\n' if body.strip() else ''


def _object_end(text):
    """Index just past the brace-balanced object opening `text`, or None."""
    depth, in_str, esc = 0, False, False
    for i, c in enumerate(text):
        if esc:
            esc = False
        elif c == '\\':
            esc = True
        elif c == '"':
            in_str = not in_str
        elif not in_str and c in '{}':
            depth += 1 if c == '{' else -1
            if depth == 0:
                return i + 1
    return None


def _leak_field_value(raw):
    """One field of an envelope that would not parse: tag debris off the
    end, and one level of JSON escaping undone when it carries any."""
    val = _LEAK_TAIL_RE.sub('', raw)
    return _unescape_json_body(val) if '\\"' in val or '\\n' in val else val


def _leak_envelope_fields(rest):
    """Fields of an `arguments` envelope rebuilt from leaked text: the
    header object, plus any raw body after it as `content`; an object
    that never closes is read field by field."""
    obj = _balanced_json(rest, 0) if rest.startswith('{') else None
    if obj is None:
        # The object was a string inside a string, so its values are
        # still JSON-escaped once (`\\"\\"\\"Pop the dialog`), and a
        # field may end at the model's own close tag instead of a comma
        # (`...</old>"new": "`): space-bunny-alpha's edit of
        # catan/gui/app.py lost its whole `new` into `old` (2026-09-30).
        return {m.group(1): _leak_field_value(m.group(2))
                for m in _LEAK_OBJ_FIELD_RE.finditer(rest)}
    out = dict(obj)
    body = _leak_body(rest[_object_end(rest) or len(rest):].lstrip('}'))
    if body and 'content' not in out:
        out['content'] = body
    return out


_LEAK_BARE_KEY_RE = re.compile(r'^\s*"?(\w+)"?\s*:?\s*$')


def _leak_whole_object(text):
    """The object the model meant when the key holds SEVERAL fields --
    `limit": 10, "query": "..."}}</tool_call` is `{"limit": 10, "query":
    ...}` minus its opening -- or None. Tried before the field-by-field
    reading, which took the first field's value to the end of the text
    (`limit = '10, "query": ...'`). An object that does not balance, or
    is not a flat field set, falls through."""
    if text.lstrip().startswith(('{', '"')):
        return None
    obj = _balanced_json('{"' + text, 0)
    if not isinstance(obj, dict) or len(obj) < 2:
        return None
    return obj


def _salvage_swallowed_value(args):
    """One field whose VALUE swallowed the rest of the object, rebuilt:
    `{"limit": " 8, \\"query\\": \\"...\\"}</tool_call>"}` is limit and
    query, `{"$text": " \\"arguments\\": {\\"command\\": ...}}"}` is an
    envelope with the call inside. Same upstream as the tag leak
    (space-bunny-alpha on openrouter's Stealth, 2026-09-29): six of one
    afternoon's eleven unresolved calls. Run only AFTER validation has
    failed, so a clean argument holding JSON-looking text is never read
    this way. None when neither reading parses."""
    if not (isinstance(args, dict) and len(args) == 1):
        return None
    (key, val), = args.items()
    if not isinstance(val, str) or not re.search(r'"\w+"\s*:', val):
        return None
    name = str(key).strip('"$')
    brace = val.find('{')
    texts = ['{"%s": %s' % (name, val.lstrip()), '{' + val.lstrip()]
    if brace > 0:
        # `<arguments:{"command": ...}}</arguments>` -- a tag before the
        # object (space-bunny-alpha, 2026-09-30).
        texts.append(val[brace:])
    for text in texts:
        obj = _balanced_json(text, 0) or _closed_object(text)
        if isinstance(obj, dict) and isinstance(obj.get('arguments'), dict) \
                and 'name' in obj:
            # The value held a whole call: `{"name": "bash", "arguments":
            # {...}}` under `$text` (catan, 2026-09-30) -- its arguments.
            obj = _call_args_from_obj(obj)
        if isinstance(obj, dict) and obj and obj != args:
            return _unwrap_nested_arguments(obj)
    return None


def _closed_object(text):
    """`text` parsed after dropping a bare word stuck past its last close
    (`...\"}edit`) and closing the braces it never wrote -- an envelope
    under `$text` that lost one `}` (catan gui/audio.py, 2026-09-30)."""
    trimmed = re.sub(r'(?<=[}"])\w+\s*$', '', text.rstrip())
    closed = _close_unterminated_object(trimmed)
    try:
        obj = json.loads(closed) if closed else None
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _leak_bare_key(name, val):
    """A bare name with debris (`old":`): its value as given, unless the
    value swallowed the rest of the object -- `from x import y", "path":
    "gui/app.py", "new": "..."}}` read as ONE object, so path and new
    come back (space-bunny-alpha, 2026-09-30, an edit of gui/app.py)."""
    if isinstance(val, str) and re.search(r'",\s*"\w+"\s*:', val):
        whole = _balanced_json('{"%s": "%s' % (name, val), 0)
        if isinstance(whole, dict) and len(whole) > 1:
            return whole
    return {name: val}


def _leak_item(key, val):
    """One quote-bearing key and its value, rebuilt into real fields:
    an envelope (`arguments": {...`), a bare name with debris (`old":`
    whose value is already right), or a field spelled into the key
    (`command": "cd x ...`). None when it is none of these."""
    extra = val if isinstance(val, dict) else {}
    text = key + ('>' + val if isinstance(val, str) else '')
    whole = _leak_whole_object(text)
    if whole is not None:
        return dict(whole, **{k: v for k, v in extra.items() if k not in whole})
    env = _LEAK_ENVELOPE_RE.match(text)
    bare = None if env else _LEAK_BARE_KEY_RE.match(key)
    field = None if env or bare else _LEAK_FIELD_RE.match(text)
    if env:
        out = _leak_envelope_fields(text[env.end():])
    elif bare:
        return _leak_bare_key(bare.group(1), val)
    elif field:
        out = {field.group(1): _LEAK_TAIL_RE.sub('', field.group(2))}
    else:
        return None
    out.update({k: v for k, v in extra.items() if k not in out})
    return out


def _salvage_tag_leak(args):
    """Arguments whose XML call envelope was spliced into JSON, rebuilt.

    A model trained on `<invoke name="bash"><parameter name="command">
    ...</parameter>` (MiniMax lineage: its own `]<]minimax[` token
    leaked in one specimen) comes back through a provider's tool parser
    with every `>` it wrote rendered as `": "`. The JSON still PARSES --
    into one key that holds the whole call:

        {'command": "cd /w && make 2': '&1|tail -1'}   # was 2>&1
        {'command": "cat x.py</command': ''}
        {'arguments": {"path": "P</arguments': {'content': 'BODY'}}
        {'arguments"': '{"path":"P"}\n```\n\nBODY'}

    or several keys at once (`{'old":': 'OLD', 'path":"P</path': {'new':
    'NEW'}}`), each repaired on its own. Every such call failed
    validation as `requires argument "command"`
    and the turn was lost: sixteen of them in the first twenty minutes
    of space-bunny-alpha building catan (2026-09-29). Joining key and
    value on `>` restores the text the model wrote; the field or
    envelope is read back out of that. A key holding a quote is never
    a real argument name, so the gate costs a clean call nothing.
    Returns the rebuilt dict, or None when the shape is not this."""
    if not isinstance(args, dict) or not any(
            isinstance(k, str) and '"' in k for k in args):
        return None
    out = {}
    for key, val in args.items():
        fixed = (_leak_item(key, val) if isinstance(key, str) and '"' in key
                 else {key: val})
        if fixed is None:
            return None
        out.update({k: v for k, v in fixed.items() if k not in out})
    if not out:
        return None
    _record('tag_leak', 'recovered', fields=','.join(sorted(out)))
    return {k: _leak_unescaped(k, v) for k, v in out.items()}


def _leak_unescaped(field, val):
    """A body field that arrived still JSON-escaped, decoded once more.

    The model sometimes double-escapes the object inside its leaked
    envelope, so one decode leaves `\\n` and `\\"` in the body: a live
    salvage wrote an 11,630-byte test file whose every line was one
    literal line, and the model had to delete it and write it again
    (2026-09-29, catan). Only when EVERY newline is literal -- a body
    mixing real and literal newlines is ambiguous and left alone, the
    same rule `_recover_broken_string` keeps."""
    if field not in ('content', 'old', 'new') or not isinstance(val, str):
        return val
    probe = val.rstrip('\n')
    if '\n' in probe or '\\n' not in probe:
        return val
    return _unescape_json_body(val)


def _unwrap_nested_arguments(args_obj, depth=3):
    """{"arguments": "{\"command\": ...}"} is a call whose arguments
    were wrapped in an arguments envelope once more -- qwen3.8 on
    2026-09-20 did it four turns in a row, every bash call landed
    unresolved (no `command`), and the at-bat ended in an empty turn.
    A dict whose ONLY key is an arguments-shaped word, holding a JSON
    string or a dict, is that inner value; bounded, and a shape that
    is not exactly this is left alone."""
    for _ in range(depth):
        if not (isinstance(args_obj, dict) and len(args_obj) == 1):
            return args_obj
        (key, inner), = args_obj.items()
        if key not in _NESTED_ARGUMENT_KEYS:
            return args_obj
        if isinstance(inner, str):
            inner = _loads_envelope_inner(inner)
            if inner is None:
                return args_obj
        if not isinstance(inner, dict):
            return args_obj
        _record('nested_arguments', 'unwrapped', key=key)
        args_obj = inner
    return args_obj


def _native_call_args(raw_args, leak_args=None):
    """Normalize the wire form of `arguments` into an argument set.

    `arguments` arrives as a JSON-encoded STRING (the OpenAI wire
    form) or as a bare object. A string that will not parse is handed
    on UNTOUCHED rather than discarded — a truncated or unescaped one
    is exactly the shape the purify layer exists to salvage, and
    losing the bytes here would make that salvage impossible.

    `leak_args` is applied only when the parser left the real
    arguments empty: arguments the provider actually delivered are
    always the better evidence than ones scraped out of a name."""
    if isinstance(raw_args, str):
        text = raw_args.strip()
        try:
            raw_args = json.loads(text) if text else {}
        except (ValueError, json.JSONDecodeError):
            return raw_args
    if leak_args is not None and not raw_args:
        return leak_args
    return raw_args


# ── Tool Call Parsing ─────────────────────────────────────────────
# Lone surrogate code points (U+D800–U+DFFF) are invalid in UTF-8 and
# crash json.dumps inside upstream SDKs. Byte-level reasoning models
# (Kimi, GLM, Xiaomi/mimo) can leak them in reasoning output that flows
# into next-turn arguments. Ported from hermes-agent
# agent/message_sanitization.py.
_SURROGATE_RE = re.compile(r'[\ud800-\udfff]')


def _sanitize_surrogates(text):
    """Replace lone surrogate code points with U+FFFD. No-op when absent."""
    if _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub('�', text)
    return text


def _escape_invalid_chars_in_json_strings(raw):
    """Escape unescaped control chars (0x00-0x1F) inside JSON string values.

    Walks char-by-char, tracking whether we are inside a double-quoted
    string. Inside strings, replaces literal control chars that aren't
    part of an escape sequence with their ``\\uXXXX`` form. Pass-through
    everywhere else. Catches llama.cpp / quantized backends emitting raw
    tabs/newlines inside JSON args. Ported from hermes-agent."""
    out = []
    in_string = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if in_string:
            if ch == '\\' and i + 1 < n:
                out.append(ch)
                out.append(raw[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
            elif ord(ch) < 0x20:
                out.append(f'\\u{ord(ch):04x}')
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_string = True
            out.append(ch)
        i += 1
    return ''.join(out)


def _repair_json(s):
    """Best-effort JSON repair for common LLM output defects.

    Handles:
    - Lone surrogate code points → U+FFFD
    - Unescaped control chars inside JSON strings → \\uXXXX
    - Invalid escape sequences: \\( \\) \\' \\! etc → remove backslash
    - Single-quoted strings → double-quoted (simple cases)
    - Trailing commas before } or ]
    """
    # Key-quote corruption — `"path>` / `"command">` where `"key":`
    # was meant, at key position with the value on the next line
    # (template-leak spelling, 2026-08-05 mining: read/grep/bash
    # heads). Normalize the key only; the missing-quote rule below
    # then rescues the bare value. Key-position anchor ([{,]) keeps
    # `">` inside HTML/XML payload strings untouched.
    s = re.sub(r'([{,]\s*)"([A-Za-z_]\w*)"?>(?=\s*\n)', r'\1"\2":', s)
    # Doubled name key — `{"name="name": "ls"` — the fused key=value
    # template (`"name=`) collided with the JSON key (`"name"`), leaving
    # a broken `"name="name":`. Collapse to a single `"name":` (qwen 2048
    # v2 -i, session 64003932, 2026-08-09: recurred across ls and
    # delegate heads within two minutes). Runs BEFORE the fused-key rules
    # below, which would otherwise turn it into `"name": "name":`.
    s = re.sub(r'([{,]\s*)"name=\s*"name"\s*:', r'\1"name":', s)
    # Colon-equals — `{"name":="edit",` — both separators at once, from
    # the XML `name="edit"` spliced into JSON (space-bunny-alpha,
    # 2026-09-29). `":=` is never valid JSON; the colon is the one kept.
    s = re.sub(r'([{,]\s*"[A-Za-z_]\w*")\s*:\s*=\s*', r'\1: ', s)
    # Name fused into one string — `{"name=done"}` / `{"name=bash}` —
    # the model welded key, `=`, and tool name into a single (often
    # unterminated) string literal. Name-ish keys only, identifier
    # values only.
    s = re.sub(r'([{,]\s*)"(name|function|tool)\s*=\s*([a-z_]+)"?',
               r'\1"name": "\3"', s)
    # Fused quote-key-equals with a quoted value — `{"name="bash",` /
    # `{"function="grep",` — the key kept its opening quote, lost its
    # closing one, and `=` stands where `:` was meant.
    s = re.sub(r'([{,]\s*)"([A-Za-z_]\w*)\s*=\s*(?=")', r'\1"\2": ', s)
    # Bare key with `=` for `:` — `{command="cd .."}` (same mining
    # round). Requires a quoted value right after, so shell payload
    # like `FOO=bar` inside a broken string never matches.
    s = re.sub(r'([{,]\s*)([A-Za-z_]\w*)\s*=\s*(?=")', r'\1"\2": ', s)
    # Doubled key-colon — `"path":` newline `": "value"` — the model
    # closed the key, then reopened a stray `": ` before the real
    # quoted value (qwen QA session 2507a1c6, 2026-08-09:
    # `{"path":\n": "/home/…/build.py"}`). The interposed newline
    # anchors it so a legitimate one-line value of `": "` is untouched;
    # collapse the stray pair back onto the key. Idempotent — the
    # repaired `"key": "value"` no longer presents a `":` after the
    # colon.
    s = re.sub(r'("\w+"\s*:)\s*\n\s*":\s*"', r'\1 "', s)
    # Fully-unquoted path value — `"path": /tmp/a.py,` — no quote on
    # either side. Path-shaped (leading /) so nothing else matches.
    s = re.sub(r'(:\s*)(/[^"\s,}]+)(\s*[,}])', r'\1"\2"\3', s)
    # Missing OPENING quote on a string value — `"key":` newline, bare
    # text, then the closing quote IS present (`"command":\nwc -l x"`).
    # Two live specimens in one harvested run (qwen, 2026-08-04).
    # Anchor on the orphaned closing quote; runs FIRST so the
    # control-char pass below then escapes the value's raw newlines
    # like any other string. Values opening with [ { or " are left
    # alone, which also keeps this idempotent.
    s = re.sub(
        r'("[A-Za-z_]\w*"\s*:)\s*\n\s*([^"\s\[{][\s\S]*?)"(\s*[,}])',
        lambda m: m.group(1) + ' "' + m.group(2) + '"' + m.group(3),
        s)
    # Arguments left OUTSIDE the object — `{"name="bash"} command=python
    # -m x --flag y` (vault fleet 2026-08-19): the object closed after
    # the name and the real argument trailed as bare `key=value` text.
    # Fold the tail back in as a proper member. Key must be
    # identifier-shaped and the tail brace-free, so a legitimate blob
    # whose LAST value merely ends near a `}` never matches; json.dumps
    # supplies quoting. Idempotent: the repaired form has no text after
    # its final brace.
    m = re.search(r'^(\s*\{[\s\S]*\})\s*([A-Za-z_]\w*)\s*=\s*([^{}]+?)\s*$',
                  s)
    if m and not m.group(1).rstrip().endswith(','):
        _obj, _key, _val = m.group(1).rstrip(), m.group(2), m.group(3)
        s = (_obj[:-1].rstrip().rstrip(',')
             + f', "{_key}": ' + json.dumps(_val) + '}')
    # NOTE deliberately absent: unescaped interior quotes in a WRITE
    # body (`"content": "... --uri "http://x" ..."`) are owned by the
    # dedicated write-call salvager (see TestWriteCallSalvage), which
    # understands escape semantics and compile-checks recovered .py
    # bodies. A repair-level re-encode here intercepted those calls
    # first and mangled their \n escapes — tried and reverted
    # 2026-08-19; do not re-add.
    # Fully-unquoted FILE BODY — `"content": #!/bin/bash\n...}}` — the
    # model attached a large raw body with no quote at either end
    # (vault fleet 2026-08-19: 32 malformed write calls in one night,
    # each costing a recovery turn, several landing as 0-byte files).
    # Body keys only, and only when the bare value runs to the blob's
    # closing braces — the shape these emissions share, content being
    # the last argument — so a well-formed value elsewhere in the blob
    # never matches. Greedy capture backtracks to the LAST `}}`, so
    # braces inside the body survive. json.dumps supplies quoting and
    # escaping; a repaired value starts with a quote, which the
    # negative lookahead rejects — idempotent.
    m = re.search(
        r'("(?:content|body|text)"\s*:\s*)'
        r'(?!\s*["\[{]|\s*(?:true|false|null)\s*[,}]|\s*-?\d[\d.eE+-]*\s*[,}])'
        r'([\s\S]+)(}\s*}\s*$)',
        s)
    if m:
        s = s[:m.start()] + m.group(1) + json.dumps(m.group(2).rstrip()) \
            + m.group(3)
    s = _sanitize_surrogates(s)
    s = _escape_invalid_chars_in_json_strings(s)
    # Remove backslash before characters that aren't valid JSON escapes
    # Valid: \\ \" \/ \b \f \n \r \t \uXXXX
    valid_escapes = {'"', '\\', '/', 'b', 'f', 'n', 'r', 't', 'u'}
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s) and s[i + 1] not in valid_escapes:
            # Drop the backslash, keep the next char
            i += 1
        else:
            out.append(c)
            i += 1
    s = ''.join(out)
    # Remove trailing commas before } or ]
    s = re.sub(r',\s*([}\]])', r'\1', s)
    return s


_ARG_WRAPPERS = ('arguments', 'args', 'input', 'inputs', 'parameters',
                 'params', 'kwargs', 'payload', 'tool_input')


_UNOPENED_STR_RE = re.compile(r'(:\s*)(/[^"\s,}]*)(")')


def _reopen_unquoted_values(blob):
    """Restore the opening quote on `"path": /home/x.py"`.

    A dropped opening quote is the most common way a live emission
    fails to parse while stating its intent perfectly — the closing
    quote is right there.
    """
    return _UNOPENED_STR_RE.sub(lambda m: m.group(1) + '"' + m.group(2) + '"',
                                blob)


def _balanced_json(text, start):
    """Parse the brace-balanced object starting at `text[start]`.

    Braces inside string literals do not count. Returns the dict, or
    None when it never balances (a truncated emission) or does not
    parse even after repair.
    """
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == '\\':
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                blob = text[start:i + 1]
                for candidate in (blob, _repair_json(blob),
                                  _repair_json(_reopen_unquoted_values(blob))):
                    try:
                        obj = json.loads(candidate)
                    except Exception:
                        continue
                    if isinstance(obj, dict):
                        return obj
                return None
    return None


_PATH_SHELL_ESCAPES = (('\\ ', ' '), ('\\(', '('), ('\\)', ')'),
                       ('\\&', '&'), ('\\;', ';'), ("\\'", "'"),
                       ('\\"', '"'))


_PATH_WRECKAGE_RE = re.compile(r'[\s"\'{}\[\]<>]+$')


# XML-attribute debris on the FRONT of a path: a native-format emission
# welded into JSON leaves `="report.html">` as a path value (qwen 27B,
# vault fleet 2026-08-19 — the embedded-param split recovered `content`
# but the host kept its attribute husk, and a file literally named
# `="report.html">` is what write would have created). Equals signs and
# quotes never START a path a model means.
_PATH_LEADING_HUSK_RE = re.compile(r'^[\s"\'=]+')


def _strip_json_wreckage(p):
    """Drop JSON/XML structure a broken parse left on a path.

    A model's unparseable `write` produced the path
    `scripts/crap.py"}\n}` and the harness CREATED that file — an
    empty file whose name is JSON debris (fox QA session abf87e6b,
    2026-08-07). Quotes, braces, brackets, angle brackets and newlines
    are never part of a path a model means; they are always parse
    artifacts, so they come off the end — and `="` husks off the front
    (the attribute-shaped remnant of a native-XML emission). The
    interior is left alone: a filename may legitimately contain almost
    anything, and only the edges carry the wreckage.
    """
    if not isinstance(p, str):
        return p
    cleaned = _PATH_WRECKAGE_RE.sub('', p)
    cleaned = _PATH_LEADING_HUSK_RE.sub('', cleaned)
    # A newline anywhere means the "path" swallowed the rest of the
    # emission — keep only the first line.
    if '\n' in cleaned:
        cleaned = cleaned.split('\n', 1)[0]
        cleaned = _PATH_WRECKAGE_RE.sub('', cleaned)
    return cleaned or p


def _dequote_path(p):
    """Strip quote/shell artifacts a model wraps a path in: one layer of
    matching surrounding quotes (' " `) and backslash-escaped shell
    metacharacters (`\\ ` → space, `\\(` → `(`, …). Conservative — acts
    only on unambiguous artifacts, so real paths are untouched. Returns a
    non-empty result or the original if stripping would empty it.

    Motivated by goose's shell-escaped/spaced image-path fixes: a model
    that echoes the user's quoted path (`'/a b.png'`) otherwise 404s."""
    if not isinstance(p, str):
        return p
    t = p.strip()
    if (len(t) >= 2 and t[0] == t[-1] and t[0] in ('"', "'", '`')
            and t[1:-1]):
        t = t[1:-1]
    if '\\' in t:
        for esc, ch in _PATH_SHELL_ESCAPES:
            if esc in t:
                t = t.replace(esc, ch)
    return t or p


def _strip_code_fence(s):
    """Strip a markdown code fence wrapping an entire string, returning the
    inner body (a leading ```json / ```JSON language tag is dropped). Only
    fires when the WHOLE string is fenced — `` ```json\\n{...}\\n``` extra ``
    is left untouched so we never mis-recover a partially-fenced payload.
    Idempotent; returns the input unchanged when no wrapping fence exists.

    Ported from goose's strip_json_wrapper — smol models (GLM/Minimax and
    friends) routinely wrap tool-call JSON in a code fence."""
    t = s.strip()
    if not (t.startswith('```') and t.endswith('```') and len(t) >= 6):
        return s
    inner = t[3:-3].strip()
    nl = inner.find('\n')
    if nl != -1:
        first = inner[:nl].strip()
        if first.isalpha() and len(first) <= 12:  # language tag line
            inner = inner[nl + 1:]
    else:
        for tag in ('json', 'JSON'):
            if inner.startswith(tag):
                inner = inner[len(tag):]
                break
    return inner.strip()


def _json_candidates(s):
    """The readings of a string argument worth one json.loads each: as
    sent, with invalid in-string characters escaped, de-fenced (only
    when the WHOLE arg is fenced, so a fenced `content` payload is left
    alone), and with an unterminated object closed."""
    defenced = _strip_code_fence(s)
    out = [s, _escape_invalid_chars_in_json_strings(s)]
    if defenced != s:
        out += [defenced, _escape_invalid_chars_in_json_strings(defenced)]
    closed = _close_unterminated_object(s)
    if closed:
        out.append(closed)
    doubled = _double_invalid_escapes(s)
    if doubled != s:
        out.append(doubled)
    return out


_JSON_ESCAPE_RE = re.compile(r'\\\\|\\(?![\"\\/bfnrtu])')


def _double_invalid_escapes(s):
    """`s` with each backslash that starts no valid JSON escape doubled,
    so the text keeps its literal backslash: a grep alternation `a\\|b`
    inside a command made the whole argument object unparseable
    (space-bunny-alpha, catan, 2026-09-30). Dropping the backslash
    instead would silently change what the shell runs. Existing `\\\\`
    pairs are kept as they are."""
    return _JSON_ESCAPE_RE.sub(
        lambda m: m.group(0) if len(m.group(0)) == 2 else '\\\\', s)


def _close_unterminated_object(s):
    """`s` with the closing braces it never wrote, or None. Only when the
    text stops OUTSIDE a string with objects still open -- `{"path":
    "a.py", "offset": 240, "limit": 35` (Qwen3.8 27B, 2026-09-29, read
    came out `{}`). A text cut inside a string is not closed: its last
    value is unknowable."""
    if not s.startswith('{'):
        return None
    depth, in_str, esc = 0, False, False
    for c in s:
        if esc:
            esc = False
        elif c == '\\':
            esc = True
        elif c == '"':
            in_str = not in_str
        elif not in_str and c in '{}':
            depth += 1 if c == '{' else -1
    if in_str or depth <= 0:
        return None
    return s.rstrip().rstrip(',') + '}' * depth


# A string that OPENS a JSON object and immediately names a quoted
# key. Nothing a model means as a bare value — a shell command, a
# path, an answer — starts that way, so the shape is proof the model
# was emitting structure, not a value. Kept deliberately narrow: a
# bash `{ echo hi; }` carries no quoted key and stays a bare value.
_BROKEN_JSON_OBJECT_RE = re.compile(r'^\{\s*"[^"]*"\s*:')


def _strip_dialect_tokens(out):
    """MiniMax's `]<]minimax[>[` token, which is never part of any value:
    `cat /tmp/mcts_out.txt]<]minimax[>[` read a file that does not exist
    (2026-09-30, catan). Top-level strings only; idempotent."""
    for k, v in list(out.items()):
        if isinstance(v, str) and 'minimax' in v:
            out[k] = _MINIMAX_TOKEN_RE.sub('', v)
    return out


def _call_args_from_obj(obj):
    """Extract the args payload from a parsed tool-call object.

    Canonical shape is {"name": ..., "arguments": {...}}, but models
    wander: a wrapper synonym ({"name": .., "parameters": {...}} /
    "input" / "tool_input" / ...), args inlined beside the name
    ({"name": "bash", "command": "ls"}), or an empty "arguments" next
    to the real inline args. `obj.get('arguments', {})` silently
    dropped all of those — tool name parsed, args arrived empty, and
    the harness executed `bash({})` with no evidence of what was lost
    (WSL field report 2026-07-28). Return the raw non-canonical
    payload and let purify_tool_args normalize it — passing the raw
    shape keeps the harvest evidence intact."""
    args = obj.get('arguments')
    if isinstance(args, (dict, list, str)) and args:
        return args
    rest = {k: v for k, v in obj.items()
            if k != 'name' and not (k in _ARG_WRAPPERS and not v)}
    return rest or {}


def _unescape_json_body(raw):
    """Undo the JSON escapes that survived in a salvaged string value.
    Backslash first (via a placeholder) so `\\n` in the file body is not
    re-expanded into a newline."""
    return (raw.replace('\\\\', '\x00').replace('\\n', '\n')
               .replace('\\t', '\t').replace('\\"', '"').replace('\x00', '\\'))


# The invoke dialect: `<invoke name="bash"><parameter name="command">ls
# </parameter></invoke>` -- the calling convention space-bunny-alpha
# (MiniMax lineage) is trained on, and Qwen's `<parameter=k>` spelling of
# it. It was not read at all: the clean form parsed to nothing, and
# MiniMax's `]<]minimax[>[` token wedged between every tag made the rest
# malformed (2026-09-29, catan: `="bash">...<cmd>tree -a -L 3</cmd>`,
# and Qwen's `<parameter=name>bash</parameter><parameter=command>...`
# with its whole command intact).
_MINIMAX_TOKEN_RE = re.compile(r'\]?<\]minimax\[>?\[?')


def repair_arguments(raw, primary_key=None, aliases=None):
    """`arguments` as a model sent them (a JSON string or an object) made
    into the argument object it meant. Returns (args, repairs): args a dict
    ({} when nothing could be recovered), repairs the names of the repairs
    that fired, in order, e.g. ["json_repair", "nested_arguments"].

    primary_key: the field a bare, non-JSON value belongs to (a shell tool's
    "command"); without one a bare value is dropped. aliases: {wrong: right}
    key renames for this tool. A string that is plainly a JSON object no
    repair could parse gives {} rather than stuffing its debris into the
    primary key: the tool can then name what it wanted."""
    token = _noted.set([])
    try:
        args = _repair(raw, primary_key, aliases or {}, 0)
        return args, list(_noted.get())
    finally:
        _noted.reset(token)


def _repair(raw, primary_key, aliases, depth):
    if depth > 4:
        return raw if isinstance(raw, dict) else {}
    if isinstance(raw, str):
        return _repair_text(raw, primary_key, aliases, depth)
    if isinstance(raw, list):
        return _repair_list(raw, primary_key, aliases, depth)
    if not isinstance(raw, dict):
        return {primary_key: raw} if (primary_key and raw is not None) else {}
    return _repair_object(raw, primary_key, aliases, depth)


def _repair_text(raw, primary_key, aliases, depth):
    """A string: JSON as sent, else each repair candidate, else a bare value."""
    text = _sanitize_surrogates(raw.strip())
    if not text:
        return {}
    try:
        return _repair(json.loads(text), primary_key, aliases, depth + 1)
    except (ValueError, json.JSONDecodeError):
        pass
    # The gentle readings first; the full repair (trailing commas, single
    # quotes, broken keys: the text-call path's) only when all fail.
    for candidate in _json_candidates(text) + [_repair_json(text)]:
        try:
            parsed = json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            continue
        _record("json_repair", "recovered")
        return _repair(parsed, primary_key, aliases, depth + 1)
    if _BROKEN_JSON_OBJECT_RE.match(text):
        _record("json_repair", "refused")
        return {}
    return {primary_key: raw} if primary_key else {}


def _repair_list(raw, primary_key, aliases, depth):
    """[{key,value}|{name,value}] pairs folded, a singleton unwrapped."""
    folded = {
        str(el.get("key", el.get("name"))): el["value"]
        for el in raw
        if isinstance(el, dict) and "value" in el and ("key" in el or "name" in el)
    }
    if folded:
        _record("key_value_pairs", "recovered")
        return _repair(folded, primary_key, aliases, depth + 1)
    if len(raw) == 1:
        return _repair(raw[0], primary_key, aliases, depth + 1)
    return {primary_key: raw} if primary_key else {}


def _unwrapped(raw):
    """The object inside a {"key":K,"value":V} pair or a lone wrapper key
    (arguments/input/params/...), or None when `raw` is the object itself."""
    if set(raw) in ({"key", "value"}, {"name", "value"}):
        _record("key_value_pairs", "recovered")
        return {str(raw.get("key", raw.get("name"))): raw["value"]}
    if len(raw) == 1:
        only = next(iter(raw))
        if only in _ARG_WRAPPERS and isinstance(raw[only], (dict, list, str)):
            _record("wrapper_key", "recovered")
            return raw[only]
    return None


def _repair_object(raw, primary_key, aliases, depth):
    """An object: leaked tags & nested arguments salvaged, wrappers
    unwrapped, aliases renamed, a path's quoting & debris stripped."""
    raw = _unwrap_nested_arguments(_salvage_tag_leak(raw) or raw)
    if not isinstance(raw, dict):
        return _repair(raw, primary_key, aliases, depth + 1)
    inner = _unwrapped(raw)
    if inner is not None:
        return _repair(inner, primary_key, aliases, depth + 1)
    out = {}
    for k, v in raw.items():
        ck = aliases.get(k, k)
        if not (ck in out and ck != k):
            out[ck] = v
    if isinstance(out.get("path"), str):
        path = _strip_json_wreckage(_dequote_path(out["path"]))
        out["path"] = _LEAK_TAIL_RE.sub("", path)
    return _strip_dialect_tokens(out)
