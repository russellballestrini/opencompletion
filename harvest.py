"""Our tool-call repair evidence, harvested from tool trails & room chains.

Each finished tool call is kept twice. Its reply's `Message.tool_trail`
(tools/loop.py) holds `repaired` & `repaired_from` when toolcall_repair
mended its arguments, `unparsed` when nothing could read them, `status:
error` & `error` when the tool failed. Its room's chain (chain.py,
`room_event`) holds a `tool` event with its status, error & repair marks
(`repaired` names, `unparsed: true`; never the arguments). A deleted
room's messages go, trails with them; its chain stays. This module only
reads both, so a harvest is derived & regenerable, never a second copy to
keep in step.

`make harvest` writes three lanes, shaped like uncloseai-cli's repair lane
(one JSON object a line), under `instance/harvest/repair/`:

    repaired.jsonl   a call our repair mended: tool, repairs, raw
    unparsed.jsonl   a call no repair could read: tool, raw
    errors.jsonl     a call whose tool failed: tool, error

Errors come from the chains, so deleted rooms count (& from trails only
for replies older than our chains). Repairs come from trails, which keep
the raw arguments, plus a deleted room's chain marks with `raw: null`.
Every row says `room_alive`. Then a report: what our repairs save, which
unparsed calls today's toolcall_repair would now read (salvaged since they
landed), the shapes of the rest (what to mine the next repair from) & the
commonest failures. Every room counts, solo rooms & superseded or deleted
messages too: a defect is evidence whoever saw it.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict

LANES = ("repaired", "unparsed", "errors")
EXAMPLE_CHARS = 120


def rows_of(message_id, room_id, model, trail_text):
    """(lane, row) for each call in one message's tool trail worth mining."""
    try:
        trail = json.loads(trail_text or "[]")
    except ValueError:
        return
    if not isinstance(trail, list):
        return
    for index, event in enumerate(trail):
        if not isinstance(event, dict) or event.get("tool") == "offered":
            continue
        base = {
            "model": model,
            "room": room_id,
            "message": message_id,
            "call": index,
            "tool": event.get("tool"),
            "result_sha256": event.get("result_sha256"),
        }
        if event.get("repaired"):
            # raw is None for a call kept before repaired_from was
            yield "repaired", dict(
                base, repairs=list(event["repaired"]), raw=event.get("repaired_from")
            )
        elif event.get("unparsed") is not None:
            yield "unparsed", dict(base, raw=str(event["unparsed"]))
            continue  # its error is "not a JSON object", already said
        if event.get("status") == "error":
            yield "errors", dict(base, error=str(event.get("error") or "")[:200])


def chain_rows(events, live_chains):
    """(lane, row) from the room chains. events: (chain, room_id,
    message_id, type, line) for `tool` & `assistant` events; live_chains:
    the chains whose room still exists (their repairs come from trails)."""
    models, calls = {}, []
    for chain, room_id, message_id, kind, line in events:
        try:
            body = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(body, dict):
            continue
        if kind == "assistant":
            models[(chain, message_id)] = body.get("model")
        elif kind == "tool":
            calls.append((chain, room_id, message_id, body))
    for chain, room_id, message_id, body in calls:
        alive = chain in live_chains
        base = {
            "model": models.get((chain, message_id)),
            "room": room_id,
            "chain": chain,
            "message": message_id,
            "tool": body.get("tool"),
            "result_sha256": body.get("resultSha256"),
            "room_alive": alive,
        }
        if body.get("status") == "error" and not body.get("unparsed"):
            yield "errors", dict(base, error=str(body.get("error") or "")[:200])
        if alive:
            continue  # its trail holds the repair, raw arguments too
        if body.get("repaired"):
            yield "repaired", dict(base, repairs=list(body["repaired"]), raw=None)
        elif body.get("unparsed"):
            yield "unparsed", dict(base, raw=None)


def shape(raw):
    """What an unparsed call looked like, coarse enough to cluster."""
    text = (raw or "").strip()
    if not text:
        return "empty"
    if "<parameter" in text or "<tool_call" in text or "</" in text:
        return "markup tags"
    if text.startswith("{"):
        if text.count("{") > text.count("}"):
            return "object cut off"
        return "object, not JSON"
    if text.startswith("["):
        return "a list"
    return "not an object"


def replay(rows):
    """Each unparsed row through today's repair: (salvaged, still) lists."""
    from tools.loop import parse_arguments

    salvaged, still = [], []
    for row in rows:
        if row["raw"] is None:
            continue  # a deleted room's: its chain keeps no arguments
        arguments, _ = parse_arguments(row["raw"], row["tool"])
        (salvaged if arguments is not None else still).append(row)
    return salvaged, still


def _write_lane(path, rows):
    """Replace one lane file whole, owner-only: rows quote what models sent."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def harvest(messages, events, live_chains, out_dir):
    """messages: (id, room_id, username, tool_trail) tuples; events &
    live_chains: as chain_rows takes them. Writes the lanes under
    out_dir/repair/ & returns a summary for report()."""
    lanes = {lane: [] for lane in LANES}
    chained = set()  # a live room's calls its chain filed, one per result
    for lane, row in chain_rows(events, live_chains):
        if row["room_alive"]:
            chained.add((row["room"], row["message"], row["result_sha256"]))
        lanes[lane].append(row)
    for message_id, room_id, model, trail_text in messages:
        for lane, row in rows_of(message_id, room_id, model, trail_text):
            # a call is one row: its chain filed this error already
            key = (room_id, message_id, row["result_sha256"])
            if lane == "errors" and row["result_sha256"] and key in chained:
                continue
            lanes[lane].append(dict(row, room_alive=True))
    repair_dir = os.path.join(out_dir, "repair")
    os.makedirs(repair_dir, mode=0o700, exist_ok=True)
    for d in (out_dir, repair_dir):  # makedirs' mode is masked; say it outright
        os.chmod(d, 0o700)
    for lane, rows in lanes.items():
        _write_lane(os.path.join(repair_dir, lane + ".jsonl"), rows)
    salvaged, still = replay(lanes["unparsed"])
    return {"dir": repair_dir, "lanes": lanes, "salvaged": salvaged, "still": still}


def report(summary):
    """The harvest as lines of text: counts & shapes, a raw head per cluster."""
    lanes = summary["lanes"]
    lines = [
        f"harvest: {summary['dir']}",
        "  "
        + ", ".join(f"{lane} {len(rows)}" for lane, rows in lanes.items())
        + " call(s)",
    ]
    repairs = Counter(name for row in lanes["repaired"] for name in row["repairs"])
    if repairs:
        lines.append("repairs that saved a call:")
        lines += [f"  {n:>5}  {name}" for name, n in repairs.most_common()]
    gone = Counter(
        lane for lane, rows in lanes.items() for row in rows if not row["room_alive"]
    )
    if gone:
        lines.append(
            "  from deleted rooms (their chains): "
            + ", ".join(f"{lane} {gone[lane]}" for lane in LANES)
        )
    if lanes["unparsed"]:
        blind = sum(row["raw"] is None for row in lanes["unparsed"])
        lines.append(
            f"unparsed: {len(summary['salvaged'])} read by today's repair, "
            f"{len(summary['still'])} still unreadable"
            + (f", {blind} with no arguments kept" if blind else "")
        )
    clusters = defaultdict(list)
    for row in summary["still"]:
        clusters[(row["tool"], shape(row["raw"]))].append(row)
    for (tool, kind), rows in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        models = sorted({str(r["model"]) for r in rows})
        lines.append(f"  {len(rows):>5}  {tool}: {kind}  (models: {', '.join(models)})")
        lines.append(f"         e.g. {rows[0]['raw'][:EXAMPLE_CHARS]!r}")
    errors = Counter((row["tool"], row["error"][:60]) for row in lanes["errors"])
    if errors:
        lines.append("tool failures:")
        lines += [
            f"  {n:>5}  {tool}: {error}" for (tool, error), n in errors.most_common(10)
        ]
    return lines
