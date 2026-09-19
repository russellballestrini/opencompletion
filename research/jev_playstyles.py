"""Jev playstyle trial: how should a jev-capable admiral use the grid?

The exact placement-density grid (jev_hunter.density_grid) decides most of
a Battleship game on its own. This trial searches for the best way to let
jev, a classifier model, take part WITH the grid always in the mix. Every
variant clears the same seeded boards solo (paired: the same fleets in the
same order, so a difference between variants is a difference in play, not
in luck), then the strongest variants play a short round robin.

Variants (all start from the same normalised grid & its top candidates):

    grid       the grid alone: argmax, random among ties. The control.
    fuse20     jev's choice over the top 6 fused at weight 0.2: near tie-break.
    fuse40     weight 0.4, the activity's current Jev Reasoner.
    fuse70     weight 0.7: jev can overturn most of the grid's order.
    jev_picks  weight 1.0: jev's favourite among the top 6 fires outright.
    top3       weight 0.4 over the top 3 candidates only.
    top10      weight 0.4 over the top 10.
    orient     fuse40, & when jev reads the wounded ship's orientation at
               p >= 0.7 the candidates on that axis are boosted before fusing.
    score      one `score` per candidate in a single call (a 0-3 rubric on
               how likely the cell holds a ship) becomes jev's distribution,
               fused at 0.4.

One jev call per turn per variant (none for `grid`). Results: one JSON with
every game row plus a summary, & a text table.

    source vars.sh && python3 research/jev_playstyles.py --boards 20 --workers 4
"""

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import battleship_arena as arena  # noqa: E402
import classifier  # noqa: E402
import jev_hunter as jh  # noqa: E402

SHIPS = jh.SHIPS
SIZE = jh.SIZE

VARIANTS = {
    "grid": dict(weight=0.0, k=6),
    "fuse20": dict(weight=0.2, k=6),
    "fuse40": dict(weight=0.4, k=6),
    "fuse70": dict(weight=0.7, k=6),
    "jev_picks": dict(weight=1.0, k=6),
    "top3": dict(weight=0.4, k=3),
    "top10": dict(weight=0.4, k=10),
    "orient": dict(weight=0.4, k=6, orient=True),
    "score": dict(weight=0.4, k=6, score=True),
    # Whole-board variants: the labels are EVERY unfired cell, not the
    # grid's top few. jev_all still sees each cell's density; jev_blind
    # sees the board only, so it measures jev alone.
    "jev_all": dict(weight=1.0, k=100, whole=True),
    "fuse_all": dict(weight=0.4, k=100, whole=True),
    "jev_blind": dict(weight=1.0, k=100, whole=True, blind=True),
}

_SCORE_RUBRIC = [
    "Unlikely to hold a ship: no live hit nearby & many ways for ships to avoid it.",
    "Plausible: some placements of ships still afloat cover it.",
    "Likely a hit: it sits beside a live hit or on a dense stretch.",
    "Almost certainly part of the wounded ship: it extends the line of live hits.",
]


def jev_score_read(grid, shots, hits, sunk_cells, remaining):
    """One call, one `score` question per candidate over the rubric →
    {cell: probability} from the expected scores, plus the same orientation
    read as jev_read."""
    cands = jh.top_candidates(grid, shots, 6)
    if not cands:
        raise classifier.ClassifierError("no candidates")
    live = set(hits) - set(sunk_cells)
    state = (
        f"Battleship, 10x10, cells 0-99 (cell = row*10 + column). Turn {len(set(shots)) + 1}. "
        f"Ships still afloat (lengths): {sorted(remaining, reverse=True)}. "
        f"Live hits not yet sunk: {sorted(live) or 'none'}.\n"
        "Board (. unknown, o miss, X live hit, # sunk):\n"
        + jh.board_ascii(shots, hits, sunk_cells)
    )
    questions = {
        f"c{c}": {
            "type": "score",
            "instructions": "How likely does this cell hold part of a ship? "
            + jh.describe_candidate(c, v, hits, sunk_cells),
            "criteria": list(_SCORE_RUBRIC),
        }
        for c, v in cands
    }
    ans = classifier.classify(state, questions)
    scores = {}
    for c, _ in cands:
        a = ans.get(f"c{c}") or {}
        if a.get("score") is None:
            raise classifier.ClassifierError(f"cell {c} got no score")
        scores[c] = float(a["score"])
    total = sum(scores.values())
    dist = {
        c: (s / total if total > 0 else 1.0 / len(scores)) for c, s in scores.items()
    }
    return dict(
        candidates=[c for c, _ in cands], dist=dist, instance=ans.get("_instance")
    )


def jev_whole_board_read(grid, shots, hits, sunk_cells, remaining, blind=False):
    """One `choice` over EVERY unfired cell. With `blind`, the criteria name
    only the cell's position & the prompt carries no density at all, so
    the answer is jev's own read of the board."""
    fired = set(shots)
    cells = [c for c in range(jh.CELLS) if c not in fired]
    if not cells:
        raise classifier.ClassifierError("no cells left")
    live = set(hits) - set(sunk_cells)
    state = (
        f"Battleship, 10x10, cells 0-99 (cell = row*10 + column). Turn {len(fired) + 1}. "
        f"Ships still afloat (lengths): {sorted(remaining, reverse=True)}. "
        f"Live hits not yet sunk: {sorted(live) or 'none'}.\n"
        "Board (. unknown, o miss, X live hit, # sunk):\n"
        + jh.board_ascii(shots, hits, sunk_cells)
    )
    if blind:
        criteria = {
            str(c): f"Cell {c} (row {c // SIZE}, column {c % SIZE})." for c in cells
        }
        instructions = (
            "You are Jev, admiral of a Battleship fleet. Choose the cell to fire on "
            "next that sinks the enemy fleet in the fewest shots."
        )
    else:
        criteria = {
            str(c): jh.describe_candidate(c, grid[c], hits, sunk_cells) for c in cells
        }
        instructions = (
            "You are Jev, admiral of a Battleship fleet. Choose the cell to fire on "
            "next that sinks the enemy fleet in the fewest shots: finish a wounded "
            "ship along its line before hunting open water; in open water prefer "
            "cells more ship placements can cover (higher density)."
        )
    d = classifier.choose(
        state, [str(c) for c in cells], instructions=instructions, criteria=criteria
    )
    return dict(
        candidates=cells,
        value=int(d["value"]),
        dist={int(c): p for c, p in d["dist"].items()},
        confidence=d["confidence"],
        orientation=None,
        orientation_p=None,
    )


def orient_boost(grid, hits, sunk_cells, orientation, p, factor=1.5):
    """Boost cells that extend the live hits along `orientation`."""
    live = set(hits) - set(sunk_cells)
    if not live or orientation not in ("horizontal", "vertical") or (p or 0) < 0.7:
        return grid
    step = 1 if orientation == "horizontal" else SIZE
    out = list(grid)
    for h in live:
        for d in (-step, step):
            c = h + d
            if 0 <= c < jh.CELLS and (step != 1 or c // SIZE == h // SIZE):
                out[c] = round(out[c] * factor)
    return out


def choose(variant, shots, hits, sunk_cells, remaining, rng):
    cfg = VARIANTS[variant]
    grid = jh.normalize(jh.density_grid(shots, hits, sunk_cells, remaining))
    weight, k = cfg["weight"], cfg["k"]
    source, error = "grid", None
    if weight > 0 and (cfg.get("whole") or jh.top_candidates(grid, shots, k)):
        try:
            if cfg.get("whole"):
                read = jev_whole_board_read(
                    grid,
                    shots,
                    hits,
                    sunk_cells,
                    remaining,
                    blind=cfg.get("blind", False),
                )
            elif cfg.get("score"):
                read = jev_score_read(grid, shots, hits, sunk_cells, remaining)
            else:
                read = jh.jev_read(grid, shots, hits, sunk_cells, remaining, k=k)
                if cfg.get("orient"):
                    grid = orient_boost(
                        grid,
                        hits,
                        sunk_cells,
                        read.get("orientation"),
                        read.get("orientation_p"),
                    )
            fused = jh.fuse(grid, read["dist"], weight=weight)
            source = "jev"
        except classifier.ClassifierError as exc:
            fused, error = grid, str(exc)[:120]
    else:
        fused = grid
    return jh.argmax_shot(fused, shots, rng), source, error


def solo(variant, seed_key):
    rng = random.Random(seed_key)
    board = jh.place_ships(rng)
    dice = random.Random(f"{seed_key}:{variant}")
    shots, hits, sunk_cells, sunk = [], [], [], set()
    jev_turns = errors = 0
    t0 = time.monotonic()
    while len(sunk) < len(SHIPS):
        remaining = [s for n, s in SHIPS.items() if n not in sunk]
        shot, source, error = choose(variant, shots, hits, sunk_cells, remaining, dice)
        jev_turns += source == "jev"
        errors += bool(error)
        shots.append(shot)
        if board[shot] != -1:
            hits.append(shot)
            name = board[shot]
            cells = [c for c, s in enumerate(board) if s == name]
            if all(c in hits for c in cells):
                sunk.add(name)
                sunk_cells.extend(cells)
    return dict(
        variant=variant,
        board=seed_key,
        shots=len(shots),
        jev_turns=jev_turns,
        errors=errors,
        seconds=round(time.monotonic() - t0, 2),
    )


def versus(va, vb, seed_key, first):
    rng = random.Random(seed_key)
    boards = {va: jh.place_ships(rng), vb: jh.place_ships(rng)}  # each side's own fleet
    sides = {}
    for v in (va, vb):
        sides[v] = dict(
            shots=[],
            hits=[],
            sunk_cells=[],
            sunk=set(),
            dice=random.Random(f"{seed_key}:{v}"),
        )
    target = {va: vb, vb: va}
    order = (va, vb) if first == "a" else (vb, va)
    winner = None
    while winner is None:
        for v in order:
            s = sides[v]
            board = boards[target[v]]
            remaining = [n2 for n, n2 in SHIPS.items() if n not in s["sunk"]]
            shot, _, _ = choose(
                v, s["shots"], s["hits"], s["sunk_cells"], remaining, s["dice"]
            )
            s["shots"].append(shot)
            if board[shot] != -1:
                s["hits"].append(shot)
                name = board[shot]
                cells = [c for c, x in enumerate(board) if x == name]
                if all(c in s["hits"] for c in cells):
                    s["sunk"].add(name)
                    s["sunk_cells"].extend(cells)
            if len(s["sunk"]) == len(SHIPS):
                winner = v
                break
    return dict(
        a=va,
        b=vb,
        first=first,
        winner=winner,
        shots={v: len(sides[v]["shots"]) for v in (va, vb)},
    )


# ── Cross play: jev playstyles against the existing game modes ─────
# battleship_modes' admirals (random, hunter, super_hunter, llm_reasoner)
# clear the same boards solo, & the strongest playstyles meet each of
# them head-to-head under a hard cap on jev calls.

_jev_calls = {"n": 0}
_jev_lock = threading.Lock()


def _count_jev(n):
    with _jev_lock:
        _jev_calls["n"] += n
        return _jev_calls["n"]


def solo_mode(mode, seed_key):
    row = arena.solo_clear(mode, seed_key, use_classifier=False)
    return dict(
        variant=mode,
        board=seed_key,
        shots=row["solo_shots"],
        jev_turns=0,
        errors=0,
        seconds=row["seconds"],
        kind="mode",
    )


def versus_mode(variant, mode, seed_key, first, cap):
    """One playstyle `variant` against one existing `mode`. Returns None
    (skipped) when the jev call budget is spent before the game starts."""
    if _jev_calls["n"] >= cap:
        return None
    rng = random.Random(seed_key)
    board_v, board_m = jh.place_ships(rng), jh.place_ships(rng)
    v = dict(
        shots=[],
        hits=[],
        sunk_cells=[],
        sunk=set(),
        dice=random.Random(f"{seed_key}:{variant}"),
    )
    m_side = arena.Side(mode, board_v, random.Random(f"{seed_key}:{mode}"))
    order = ("v", "m") if first == "a" else ("m", "v")
    winner, jev_turns = None, 0
    while winner is None:
        for who in order:
            if who == "v":
                remaining = [n2 for n, n2 in SHIPS.items() if n not in v["sunk"]]
                shot, source, _ = choose(
                    variant,
                    v["shots"],
                    v["hits"],
                    v["sunk_cells"],
                    remaining,
                    v["dice"],
                )
                if source == "jev":
                    jev_turns += 1
                    _count_jev(1)
                v["shots"].append(shot)
                if board_m[shot] != -1:
                    v["hits"].append(shot)
                    name = board_m[shot]
                    cells = [c for c, x in enumerate(board_m) if x == name]
                    if all(c in v["hits"] for c in cells):
                        v["sunk"].add(name)
                        v["sunk_cells"].extend(cells)
                if len(v["sunk"]) == len(SHIPS):
                    winner = variant
                    break
            else:
                m_side.fire(use_classifier=False)
                if m_side.done:
                    winner = mode
                    break
    return dict(
        a=variant,
        b=mode,
        first=first,
        winner=winner,
        jev_turns=jev_turns,
        shots={variant: len(v["shots"]), mode: len(m_side.state["shots"])},
    )


def run_cross(res, modes, games, top, cap, workers=4, chat_workers=2, progress=print):
    """Add the existing modes to `res`: solo on the trial's boards (no jev
    calls; the chat model at `chat_workers`) & head-to-head against the
    `top` playstyles by solo mean, `games` per pair, under `cap` jev calls."""
    seed, boards = res["config"]["seed"], res["config"]["boards"]
    keys = [f"{seed}:board:{i}" for i in range(boards)]
    _jev_calls["n"] = 0
    solo_rows = []
    fast = [m for m in modes if m != "llm_reasoner"]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row in pool.map(
            lambda j: solo_mode(*j), [(m, k) for m in fast for k in keys]
        ):
            solo_rows.append(row)
            progress(f"[solo {row['variant']:<13}] {row['shots']:>3} shots")
    if "llm_reasoner" in modes:
        with ThreadPoolExecutor(max_workers=chat_workers) as pool:
            for row in pool.map(lambda k: solo_mode("llm_reasoner", k), keys):
                solo_rows.append(row)
                progress(f"[solo llm_reasoner ] {row['shots']:>3} shots")
    res["solo"].extend(solo_rows)
    for m in modes:
        xs = sorted(r["shots"] for r in solo_rows if r["variant"] == m)
        res["summary"][m] = dict(
            n=len(xs),
            mean=round(sum(xs) / len(xs), 2),
            median=xs[len(xs) // 2],
            min=xs[0],
            max=xs[-1],
            kind="mode",
        )
    playstyles = [v for v in res["ranking"] if v != "grid"][:top]
    jobs = [
        (v, m, f"{seed}:cross:{v}:{m}:{g}", "ab"[g % 2], cap)
        for v in playstyles
        for m in modes
        for g in range(games)
    ]
    llm_jobs = [j for j in jobs if j[1] == "llm_reasoner"]
    other = [j for j in jobs if j[1] != "llm_reasoner"]
    rows = []
    for batch, w in ((other, workers), (llm_jobs, chat_workers)):
        with ThreadPoolExecutor(max_workers=w) as pool:
            for row in pool.map(lambda j: versus_mode(*j), batch):
                if row is None:
                    continue
                rows.append(row)
                progress(
                    f"[cross {row['a']:<9} v {row['b']:<13}] {row['winner']:<13} in "
                    f"{row['shots'][row['winner']]:>3}  jev calls so far {_jev_calls['n']}"
                )
    res["cross"] = rows
    res["cross_config"] = dict(
        modes=modes,
        games=games,
        playstyles=playstyles,
        cap=cap,
        jev_calls=_jev_calls["n"],
        skipped=len(jobs) - len(rows),
    )
    wins = {v: {m: 0 for m in modes} for v in playstyles}
    played = {v: {m: 0 for m in modes} for v in playstyles}
    for r in rows:
        played[r["a"]][r["b"]] += 1
        if r["winner"] == r["a"]:
            wins[r["a"]][r["b"]] += 1
    res["cross_wins"], res["cross_played"] = wins, played
    return res


def report_cross(res):
    if "cross" not in res:
        return ""
    cc = res["cross_config"]
    s = res["summary"]
    lines = [
        "",
        f"existing modes on the same {res['config']['boards']} boards "
        "(solo shots, mean / median / min / max)",
    ]
    for m in cc["modes"]:
        x = s[m]
        lines.append(
            f"  {m:<13}{x['mean']:>7}{x['median']:>8}{x['min']:>5}{x['max']:>5}"
        )
    lines += [
        "",
        f"playstyles v existing modes, {cc['games']} games per pair, "
        f"{cc['jev_calls']} jev calls (cap {cc['cap']}, {cc['skipped']} games skipped)",
    ]
    lines.append(" " * 10 + "".join(f"{m:>14}" for m in cc["modes"]) + "   won/played")
    for v in cc["playstyles"]:
        w, p = res["cross_wins"][v], res["cross_played"][v]
        lines.append(
            f"{v:<10}"
            + "".join(f"{w[m]}/{p[m]}".rjust(14) for m in cc["modes"])
            + f"{sum(w.values()):>8}/{sum(p.values())}"
        )
    return "\n".join(lines)


def run(
    boards=20,
    seed=0,
    workers=4,
    variants=None,
    versus_games=3,
    versus_top=4,
    progress=print,
):
    variants = list(variants or VARIANTS)
    keys = [f"{seed}:board:{i}" for i in range(boards)]
    jobs = [(v, k) for v in variants for k in keys]
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, row in enumerate(pool.map(lambda j: solo(*j), jobs), start=1):
            rows.append(row)
            progress(
                f"[{i:>3}/{len(jobs)}] {row['variant']:<10} {row['shots']:>3} shots  jev {row['jev_turns']:>2}  err {row['errors']}"
            )
    per = {v: [r["shots"] for r in rows if r["variant"] == v] for v in variants}
    by_board = {
        v: {r["board"]: r["shots"] for r in rows if r["variant"] == v} for v in variants
    }
    summary = {}
    for v in variants:
        xs = sorted(per[v])
        diffs = (
            [by_board[v][k] - by_board["grid"][k] for k in keys]
            if "grid" in by_board
            else []
        )
        summary[v] = dict(
            n=len(xs),
            mean=round(sum(xs) / len(xs), 2),
            median=xs[len(xs) // 2],
            min=xs[0],
            max=xs[-1],
            vs_grid=dict(
                better=sum(d < 0 for d in diffs),
                same=sum(d == 0 for d in diffs),
                worse=sum(d > 0 for d in diffs),
                mean_diff=round(sum(diffs) / len(diffs), 2) if diffs else None,
            ),
            errors=sum(r["errors"] for r in rows if r["variant"] == v),
            jev_turns=sum(r["jev_turns"] for r in rows if r["variant"] == v),
        )
    ranking = sorted(variants, key=lambda v: summary[v]["mean"])
    # Round robin among the best few (grid always included as the control).
    top = ranking[:versus_top]
    if "grid" not in top:
        top = top[:-1] + ["grid"]
    vs_rows = []
    pairs = [(a, b) for i, a in enumerate(top) for b in top[i + 1 :]]
    vjobs = [
        (a, b, f"{seed}:vs:{a}:{b}:{g}", "ab"[g % 2])
        for a, b in pairs
        for g in range(versus_games)
    ]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, row in enumerate(pool.map(lambda j: versus(*j), vjobs), start=1):
            vs_rows.append(row)
            progress(
                f"[vs {i:>2}/{len(vjobs)}] {row['a']} v {row['b']}: {row['winner']} in {row['shots'][row['winner']]}"
            )
    wins = {v: {o: 0 for o in top} for v in top}
    for r in vs_rows:
        loser = r["b"] if r["winner"] == r["a"] else r["a"]
        wins[r["winner"]][loser] += 1
    return dict(
        config=dict(
            boards=boards,
            seed=seed,
            workers=workers,
            variants=variants,
            versus_games=versus_games,
            classifier=classifier.describe() or "none configured",
            jev_weight_default=jh.JEV_WEIGHT,
            started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ),
        solo=rows,
        versus=vs_rows,
        summary=summary,
        ranking=ranking,
        versus_wins=wins,
        versus_order=top,
    )


def report(res):
    s = res["summary"]
    lines = [
        f"jev playstyles: {res['config']['boards']} boards each, seed {res['config']['seed']}",
        f"classifier: {res['config']['classifier']}",
        "",
        f"{'variant':<10}{'mean':>7}{'median':>8}{'min':>5}{'max':>5}   vs grid (better/same/worse, mean diff)   jev turns  errors",
    ]
    for v in res["ranking"]:
        x = s[v]
        g = x["vs_grid"]
        lines.append(
            f"{v:<10}{x['mean']:>7}{x['median']:>8}{x['min']:>5}{x['max']:>5}   "
            f"{g['better']:>2}/{g['same']:>2}/{g['worse']:>2}  {g['mean_diff']:>+6}   {x['jev_turns']:>7}  {x['errors']:>5}"
        )
    top = res["versus_order"]
    lines += [
        "",
        f"round robin, {res['config']['versus_games']} games per pair (row beat column)",
    ]
    lines.append(" " * 10 + "".join(f"{o:>10}" for o in top) + "   total")
    for v in top:
        lines.append(
            f"{v:<10}"
            + "".join(
                f"{('-' if o == v else res['versus_wins'][v][o]):>10}" for o in top
            )
            + f"{sum(res['versus_wins'][v].values()):>8}"
        )
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", type=int, default=20)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--versus-games", type=int, default=3)
    ap.add_argument(
        "--variants", nargs="+", default=list(VARIANTS), choices=list(VARIANTS)
    )
    ap.add_argument("--out", default="jev_playstyles.json")
    ap.add_argument(
        "--load", help="extend an existing results file instead of running the trial"
    )
    ap.add_argument(
        "--cross",
        nargs="*",
        help="existing modes to compare against (default: all four)",
    )
    ap.add_argument("--cross-games", type=int, default=3)
    ap.add_argument(
        "--cross-top", type=int, default=3, help="how many playstyles, by solo mean"
    )
    ap.add_argument(
        "--cross-cap",
        type=int,
        default=1950,
        help="hard cap on jev calls for the cross games",
    )
    ap.add_argument("--chat-workers", type=int, default=2)
    a = ap.parse_args(argv)
    if not classifier.available():
        print(
            "no classifier configured: every variant would be the grid", file=sys.stderr
        )
        return 2
    if a.load:
        with open(a.load) as f:
            res = json.load(f)
    else:
        res = run(a.boards, a.seed, a.workers, a.variants, a.versus_games)
    if a.cross is not None:
        modes = a.cross or ["random", "hunter", "super_hunter", "llm_reasoner"]
        run_cross(
            res,
            modes,
            a.cross_games,
            a.cross_top,
            a.cross_cap,
            a.workers,
            a.chat_workers,
        )
    with open(a.out, "w") as f:
        json.dump(res, f, indent=1)
    print()
    print(report(res))
    print(report_cross(res))
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
