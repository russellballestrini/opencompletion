"""Battleship arena: every targeting mode plays every other mode.

Each unordered pair of modes in `battleship_modes.MODES` plays
`games_per_pair` games. Both admirals get a fresh random fleet; they fire
alternately, the first mover alternating game by game so neither side
banks the tempo advantage; the first to sink all five ships wins. Every
mode also clears the same set of boards solo, which gives a shots-to-sink
distribution independent of who it faced.

Everything is seeded: game g of pair (a, b) draws its boards & its dice
from `Random(f"{seed}:{a}:{b}:{g}")`, so a run reproduces exactly for the
grid modes. The two model-backed modes are as reproducible as their
vendors: LLM Reasoner records whether the chat model answered on each
turn (`llm_ok`) & how often its answer was unusable (`llm_fallback`), Jev
Reasoner records whether a classifier decided (`jev`) or the grid alone
did (`grid`), so a results file always says which admiral really played.

    python3 battleship_arena.py --games 10 --seed 0 --out arena_results.json
    python3 battleship_arena.py --modes random hunter super_hunter --games 3

Results are one JSON document: `config`, `games` (one row per game),
`solo` (one row per mode per board), & `summary` (win matrix, mean shots,
model health). `make arena` runs the full round robin.
"""

import argparse
import itertools
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import battleship_modes as bm
import classifier
import jev_hunter

MODES = list(bm.MODES)


# ── One game ───────────────────────────────────────────────────────


def judge(board, shot, hits_so_far):
    """(hit, sunk_ship_name, sunk_cells) for a shot on `board`, given the
    shooter's hits so far (including this one when it hits)."""
    name = board[shot]
    if name == -1:
        return False, None, ()
    cells = [c for c, s in enumerate(board) if s == name]
    if all(c in hits_so_far for c in cells):
        return True, name, tuple(cells)
    return True, None, ()


class Side:
    """One admiral at the table: a mode, its state, & the board it is
    firing at (held by the referee, never shown to the mode)."""

    def __init__(self, mode, target_board, rng):
        self.mode = mode
        self.board = target_board
        self.rng = rng
        self.state = bm.new_state()
        self.sunk = 0
        self.jev_sources = {"jev": 0, "grid": 0}
        self.llm_ok = 0
        self.llm_fallback = 0
        self.llm_turns = 0
        self.llm_errors = {}

    def fire(self, chat=None, use_classifier=None):
        shot = bm.choose_shot(
            self.mode, self.state, self.rng, chat=chat, use_classifier=use_classifier
        )
        hits = set(self.state["hits"]) | {shot}
        hit, sunk_name, sunk_cells = judge(self.board, shot, hits)
        bm.record_shot(self.mode, self.state, shot, hit, sunk_name, sunk_cells)
        if sunk_name:
            self.sunk += 1
        if self.mode == "jev_reasoner":
            self.jev_sources[self.state["jev_read"]["source"]] += 1
        elif self.mode == "llm_reasoner":
            self.llm_turns += 1
            last = self.state["llm_last"] or {}
            self.llm_ok += 1 if last.get("ok") else 0
            self.llm_fallback += 1 if last.get("fallback") else 0
            if not last.get("ok"):
                # The failure's shape (exception name), never the prompt.
                kind = (
                    (last.get("text") or "error")[:60].split(":")[1].strip()
                    if ":" in (last.get("text") or "")
                    else "error"
                )
                self.llm_errors[kind] = self.llm_errors.get(kind, 0) + 1
            elif last.get("fallback"):
                self.llm_errors["unusable answer"] = (
                    self.llm_errors.get("unusable answer", 0) + 1
                )
        return shot, hit, sunk_name

    @property
    def done(self):
        return self.sunk >= len(bm.SHIPS)

    def row(self, prefix):
        out = {
            f"{prefix}_shots": len(self.state["shots"]),
            f"{prefix}_hits": len(self.state["hits"]),
            f"{prefix}_sunk": self.sunk,
        }
        if self.mode == "jev_reasoner":
            out[f"{prefix}_jev_sources"] = dict(self.jev_sources)
        if self.mode == "llm_reasoner":
            out[f"{prefix}_llm"] = {
                "turns": self.llm_turns,
                "ok": self.llm_ok,
                "fallback": self.llm_fallback,
                "errors": dict(self.llm_errors),
            }
        return out


def play_game(mode_a, mode_b, seed_key, first="a", chat=None, use_classifier=None):
    """One game between two modes. Returns a result row."""
    rng = random.Random(seed_key)
    board_a = jev_hunter.place_ships(rng)  # a's fleet, b fires at it
    board_b = jev_hunter.place_ships(rng)
    a = Side(mode_a, board_b, random.Random(f"{seed_key}:a"))
    b = Side(mode_b, board_a, random.Random(f"{seed_key}:b"))
    order = (a, b) if first == "a" else (b, a)
    t0 = time.monotonic()
    winner = None
    turns = 0
    while winner is None:
        turns += 1
        for side in order:
            side.fire(chat=chat, use_classifier=use_classifier)
            if side.done:
                winner = "a" if side is a else "b"
                break
    row = {
        "a": mode_a,
        "b": mode_b,
        "first": first,
        "winner": winner,
        "winner_mode": mode_a if winner == "a" else mode_b,
        "turns": turns,
        "seconds": round(time.monotonic() - t0, 3),
    }
    row.update(a.row("a"))
    row.update(b.row("b"))
    return row


def solo_clear(mode, seed_key, chat=None, use_classifier=None):
    """One mode sinks one fleet alone. Returns a result row."""
    rng = random.Random(seed_key)
    board = jev_hunter.place_ships(rng)
    side = Side(mode, board, random.Random(f"{seed_key}:solo"))
    t0 = time.monotonic()
    while not side.done:
        side.fire(chat=chat, use_classifier=use_classifier)
    row = {"mode": mode, "seconds": round(time.monotonic() - t0, 3)}
    row.update(side.row("solo"))
    return row


# ── The tournament ─────────────────────────────────────────────────


def schedule(modes, games_per_pair, seed):
    """Every unordered pair, `games_per_pair` games each, first mover
    alternating; & every mode solo on the same `games_per_pair` boards
    per pair-slot so solo & versus draw from one seeded pool."""
    games, solos = [], []
    for a, b in itertools.combinations(modes, 2):
        for g in range(games_per_pair):
            games.append(
                dict(
                    mode_a=a,
                    mode_b=b,
                    seed_key=f"{seed}:{a}:{b}:{g}",
                    first="ab"[g % 2],
                )
            )
    boards = [
        f"{seed}:solo:{i}" for i in range(games_per_pair * max(1, len(modes) - 1))
    ]
    for m in modes:
        for key in boards:
            solos.append(dict(mode=m, seed_key=key))
    return games, solos


def run(
    modes=None,
    games_per_pair=10,
    seed=0,
    workers=8,
    chat=None,
    use_classifier=None,
    progress=None,
):
    modes = list(modes or MODES)
    games, solos = schedule(modes, games_per_pair, seed)
    t0 = time.monotonic()
    done = [0]

    def _game(spec):
        row = play_game(chat=chat, use_classifier=use_classifier, **spec)
        done[0] += 1
        if progress:
            progress(done[0], len(games) + len(solos), row)
        return row

    def _solo(spec):
        row = solo_clear(chat=chat, use_classifier=use_classifier, **spec)
        done[0] += 1
        if progress:
            progress(done[0], len(games) + len(solos), row)
        return row

    with ThreadPoolExecutor(max_workers=workers) as pool:
        game_rows = list(pool.map(_game, games))
        solo_rows = list(pool.map(_solo, solos))
    results = {
        "config": {
            "modes": modes,
            "games_per_pair": games_per_pair,
            "seed": seed,
            "workers": workers,
            "classifier": classifier.describe() or "none configured",
            "llm_endpoint": os.environ.get("MODEL_ENDPOINT_1", ""),
            "llm_model": os.environ.get("MODEL_NAME_1", bm.LLM_DEFAULT_MODEL),
            "llm_timeout_s": bm.LLM_TIMEOUT_S,
            "jev_weight": jev_hunter.JEV_WEIGHT,
            "jev_candidates": jev_hunter.CANDIDATES,
            "seconds": round(time.monotonic() - t0, 1),
            "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 - 0)),
        },
        "games": game_rows,
        "solo": solo_rows,
    }
    results["summary"] = summarize(results)
    return results


def _stats(values):
    values = sorted(values)
    if not values:
        return {}
    n = len(values)
    return {
        "n": n,
        "mean": round(sum(values) / n, 2),
        "median": values[n // 2],
        "min": values[0],
        "max": values[-1],
    }


def _merge_llm(total, part):
    for k, n in part.items():
        if k == "errors":
            for kind, c in n.items():
                total["errors"][kind] = total["errors"].get(kind, 0) + c
        else:
            total[k] += n


def summarize(results):
    modes = results["config"]["modes"]
    wins = {m: {o: 0 for o in modes} for m in modes}
    played = {m: {o: 0 for o in modes} for m in modes}
    first_wins = {"first": 0, "second": 0}
    shots_to_win = {m: [] for m in modes}
    jev_sources = {"jev": 0, "grid": 0}
    llm = {"turns": 0, "ok": 0, "fallback": 0, "errors": {}}
    for g in results["games"]:
        a, b, w = g["a"], g["b"], g["winner"]
        played[a][b] += 1
        played[b][a] += 1
        wm, lm = (a, b) if w == "a" else (b, a)
        wins[wm][lm] += 1
        first_wins["first" if w == g["first"] else "second"] += 1
        shots_to_win[wm].append(g[f"{w}_shots"])
        for k in ("a", "b"):
            if f"{k}_jev_sources" in g:
                for s, n in g[f"{k}_jev_sources"].items():
                    jev_sources[s] += n
            if f"{k}_llm" in g:
                _merge_llm(llm, g[f"{k}_llm"])
    solo = {m: [] for m in modes}
    for s in results["solo"]:
        solo[s["mode"]].append(s["solo_shots"])
        if "solo_jev_sources" in s:
            for k, n in s["solo_jev_sources"].items():
                jev_sources[k] += n
        if "solo_llm" in s:
            _merge_llm(llm, s["solo_llm"])
    total_wins = {m: sum(wins[m].values()) for m in modes}
    total_played = {m: sum(played[m].values()) for m in modes}
    return {
        "wins": wins,
        "played": played,
        "win_rate": {
            m: round(total_wins[m] / total_played[m], 3) if total_played[m] else None
            for m in modes
        },
        "ranking": sorted(modes, key=lambda m: -total_wins[m]),
        "first_mover": first_wins,
        "shots_to_win": {m: _stats(v) for m, v in shots_to_win.items()},
        "solo_shots": {m: _stats(v) for m, v in solo.items()},
        "jev_sources": jev_sources,
        "llm": llm,
    }


def report(results):
    """A plain-text table of the summary."""
    s = results["summary"]
    modes = results["config"]["modes"]
    short = {m: bm.LABELS[m] for m in modes}
    width = max(len(v) for v in short.values()) + 2
    lines = [
        f"arena: {results['config']['games_per_pair']} games per pair, seed "
        f"{results['config']['seed']}, {len(results['games'])} games, "
        f"{results['config']['seconds']} s",
        f"classifier: {results['config']['classifier']}",
        "",
        "wins (row beat column)".ljust(width)
        + "".join(short[m][:8].rjust(9) for m in modes)
        + "   total  rate",
    ]
    for m in modes:
        cells = "".join(
            ("-" if o == m else str(s["wins"][m][o])).rjust(9) for o in modes
        )
        lines.append(
            short[m].ljust(width) + cells + f"{sum(s['wins'][m].values()):>8}"
            f"{(s['win_rate'][m] or 0):>6.0%}"
        )
    lines += ["", "shots to sink a fleet, solo (mean / median / min / max)"]
    for m in s["ranking"]:
        st = s["solo_shots"][m]
        if st:
            lines.append(
                f"  {short[m].ljust(width)} {st['mean']:>6} / {st['median']:>3} / "
                f"{st['min']:>3} / {st['max']:>3}   (n={st['n']})"
            )
    fm = s["first_mover"]
    lines.append(f"\nfirst mover won {fm['first']} of {fm['first'] + fm['second']}")
    js = s["jev_sources"]
    if js["jev"] or js["grid"]:
        lines.append(
            f"jev reasoner: {js['jev']} turns decided with jev, {js['grid']} by the grid alone"
        )
    ll = s["llm"]
    if ll["turns"]:
        lines.append(
            f"llm reasoner: {ll['ok']}/{ll['turns']} turns answered by the chat model, "
            f"{ll['fallback']} fell back to the grid's top cell"
            + (
                f" ({', '.join(f'{k} x{n}' for k, n in ll['errors'].items())})"
                if ll.get("errors")
                else ""
            )
        )
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--games", type=int, default=10, help="games per pair (default 10)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--modes", nargs="+", default=MODES, choices=MODES)
    ap.add_argument("--out", default="arena_results.json")
    ap.add_argument(
        "--no-classifier",
        action="store_true",
        help="pin jev reasoner to the grid alone even when a classifier is configured",
    )
    args = ap.parse_args(argv)

    def progress(n, total, row):
        who = row.get("winner_mode") or row.get("mode")
        print(f"[{n:>4}/{total}] {who:<14} {row.get('seconds')}s", flush=True)

    results = run(
        modes=args.modes,
        games_per_pair=args.games,
        seed=args.seed,
        workers=args.workers,
        use_classifier=False if args.no_classifier else None,
        progress=progress,
    )
    with open(args.out, "w") as f:
        json.dump(results, f, indent=1)
    print()
    print(report(results))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
