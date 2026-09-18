"""Battleship targeting modes, one place for the game & the arena.

`research/activity29-battleship.yaml` lets a player choose which admiral
they face. Those admirals used to live inline in the activity's
processing script, which meant an arena could only guess at them. Now the
activity & `battleship_arena.py` call the same functions, so what plays
in a room is exactly what the arena measures.

    random        any unfired cell.
    hunter        hunt/target at its best: hunts on a parity class sized
                  to the smallest ship afloat, targets the ends of a line
                  of live hits, else every neighbour of a live hit.
    super_hunter  the exact placement-density grid (jev_hunter.py):
                  every legal placement of every ship afloat, target mode
                  while a hit is live, normalised to 100; fire on the
                  maximum, ties at random.
    llm_reasoner  the same grid names its top six cells; a chat model
                  reads the board & the evidence & answers in prose
                  ("ANALYSIS: ... MOVE: n"); an unusable answer or a dead
                  endpoint fires the grid's own maximum.
    jev_reasoner  the same grid fused with one jev decision (a typed
                  choice with a probability per cell); the grid alone
                  when jev is off. So the three grid modes differ only in
                  who decides among the same candidates: nobody, a chat
                  model, or a classifier.

Each mode plays its tier's state of the art on purpose (fox, 2026-09-18):
the arena compares ideas, not implementation accidents. Before this the
inline Super Human Hunter added +5 to a hit's neighbours on top of base
densities near 34 & almost never targeted (~93 shots per fleet), & the
inline Hunter never recorded its hits at all & played as random.

A mode sees only what a player at the table would: fired cells, which of
them hit, & which ships have sunk (with their cells, the same knowledge
the sinking announcement gives away). It never sees the opponent's board.

State is a plain dict (JSON-safe, so the activity stores it in room
metadata): `new_state()` → {shots, hits, misses, sunk_ships, sunk_cells,
probability_matrix, jev_read, llm_last}. `choose_shot` picks a cell,
`record_shot` folds in what happened. Randomness comes from the `rng`
passed in, so an arena run reproduces from a seed.
"""

import os
import random
import re
import time

import jev_hunter

SIZE = 10
CELLS = SIZE * SIZE
SHIPS = dict(jev_hunter.SHIPS)
MODES = ("random", "hunter", "super_hunter", "llm_reasoner", "jev_reasoner")
# The activity's labels for each mode, as its mode-select step names them.
LABELS = {
    "random": "Random",
    "hunter": "Hunter",
    "super_hunter": "Super Human Hunter",
    "llm_reasoner": "LLM Reasoner",
    "jev_reasoner": "Jev Reasoner",
}
LLM_CANDIDATES = 6
LLM_TIMEOUT_S = 10
LLM_DEFAULT_MODEL = "adamo1139/Hermes-3-Llama-3.1-8B-FP8-Dynamic"


def new_state():
    return {
        "shots": [],
        "hits": [],
        "misses": [],
        "sunk_ships": [],
        "sunk_cells": [],
        "probability_matrix": None,
        "jev_read": None,
        "llm_last": None,
    }


def _free(st):
    fired = set(st["shots"])
    return [c for c in range(CELLS) if c not in fired]


def _neighbours(cell):
    row, col = divmod(cell, SIZE)
    out = []
    if row > 0:
        out.append(cell - SIZE)
    if row < SIZE - 1:
        out.append(cell + SIZE)
    if col > 0:
        out.append(cell - 1)
    if col < SIZE - 1:
        out.append(cell + 1)
    return out


def remaining_sizes(st):
    return [s for n, s in SHIPS.items() if n not in st["sunk_ships"]]


# ── random ─────────────────────────────────────────────────────────


def random_shot(st, rng=random):
    return rng.choice(_free(st))


# ── hunter: parity hunt, line-aware target ─────────────────────────
# The classic hunt/target player at its best. Hunting fires only on cells
# of one parity class sized to the smallest ship afloat (a checkerboard
# while the Destroyer lives), since every ship of length L crosses a cell
# with (row + col) % L == k for any k. Targeting works the live hits (hit,
# not yet sunk): two or more in a line extend that line at both ends;
# otherwise every unfired neighbour of a live hit is a target.


def live_hits(st):
    return [h for h in st["hits"] if h not in set(st["sunk_cells"])]


def line_targets(live, fired):
    """Ends of every straight run of two or more live hits, unfired."""
    live_set, out = set(live), []
    for axis_step in (1, SIZE):
        for h in live:
            prev = h - axis_step
            same_row = axis_step != 1 or prev // SIZE == h // SIZE
            if prev >= 0 and same_row and prev in live_set:
                continue  # not the start of a run
            run = [h]
            nxt = h + axis_step
            while (
                nxt < CELLS
                and nxt in live_set
                and (axis_step != 1 or nxt // SIZE == h // SIZE)
            ):
                run.append(nxt)
                nxt += axis_step
            if len(run) < 2:
                continue
            before, after = run[0] - axis_step, run[-1] + axis_step
            if before >= 0 and (axis_step != 1 or before // SIZE == h // SIZE):
                if before not in fired:
                    out.append(before)
            if after < CELLS and (axis_step != 1 or after // SIZE == h // SIZE):
                if after not in fired:
                    out.append(after)
    return sorted(set(out))


def hunter_shot(st, rng=random):
    fired = set(st["shots"])
    live = live_hits(st)
    targets = line_targets(live, fired)
    if not targets:
        targets = sorted({n for h in live for n in _neighbours(h) if n not in fired})
    if targets:
        return rng.choice(targets)
    sizes = remaining_sizes(st)
    parity = min(sizes) if sizes else 2
    free = _free(st)
    hunt = [c for c in free if (c // SIZE + c % SIZE) % parity == 0]
    return rng.choice(hunt or free)


# ── super human hunter: the exact placement-density grid ───────────
# fox's rubric: "keeps track of hits and uses a probability grid
# normalized to 100 and always picks the max or random of any 100". The
# grid is jev_hunter.density_grid: every legal placement of every ship
# afloat, target mode while a hit is live. The old inline version added
# +5 to a hit's neighbours on top of base densities near 34, so it almost
# never targeted & cleared fleets in ~93 shots, random's neighbourhood.


def super_hunter_shot(st, rng=random):
    read = jev_hunter.choose_shot(
        st["shots"],
        st["hits"],
        st["sunk_cells"],
        remaining_sizes(st),
        rng=rng,
        use_classifier=False,
    )
    st["probability_matrix"] = [
        read["grid"][r * SIZE : (r + 1) * SIZE] for r in range(SIZE)
    ]
    return read["shot"]


# ── llm reasoner: a chat model chooses among the grid's top cells ──
# Same exact grid & same six candidates Jev Reasoner sees, the board &
# per-cell evidence in the prompt, a move in prose. What differs from
# Jev Reasoner is only the decision model: a chat completion parsed for
# "MOVE: n" instead of a typed choice with probabilities.


def llm_candidates(st, k=LLM_CANDIDATES):
    grid = jev_hunter.normalize(
        jev_hunter.density_grid(
            st["shots"], st["hits"], st["sunk_cells"], remaining_sizes(st)
        )
    )
    cands = jev_hunter.top_candidates(grid, st["shots"], k)
    return grid, cands


def llm_prompt(st, grid, cands):
    top = [c for c, _ in cands]
    turn = len(st["shots"]) + 1
    afloat = ", ".join(str(x) for x in sorted(remaining_sizes(st), reverse=True))
    evidence = "\n".join(
        f"- {jev_hunter.describe_candidate(c, v, st['hits'], st['sunk_cells'])}"
        for c, v in cands
    )
    return (
        f"You are an expert Battleship admiral. Turn {turn}. Grid 10x10, cells 0-99, "
        "cell = row*10 + column.\n"
        f"Ships still afloat (lengths): {afloat}. Live hits not yet sunk: "
        f"{sorted(live_hits(st)) or 'none'}.\n\n"
        "Board (. unknown, o miss, X live hit, # sunk):\n"
        f"{jev_hunter.board_ascii(st['shots'], st['hits'], st['sunk_cells'])}\n\n"
        f"Top candidate cells by placement density (100 = most likely):\n{evidence}\n\n"
        "Finish a wounded ship along its line before hunting open water; in open "
        "water prefer the densest cell.\n\n"
        f"You MUST choose one cell from {top}.\n"
        "Format your response EXACTLY like this:\n\n"
        "ANALYSIS: [up to 3 sentences]\n\n"
        f"MOVE: [ONE number from {top}]"
    )


def chat_backends(env=None):
    """Every configured MODEL_ENDPOINT_n as (endpoint, key_env, name_env),
    the preferred one (LLM_MODEL, default MODEL_1) first, then ascending:
    the same health-aware fallback chain the app's chat path walks."""
    env = os.environ if env is None else env
    nums = [
        str(i) for i in range(100) if (env.get(f"MODEL_ENDPOINT_{i}") or "").strip()
    ]
    pref = (env.get("LLM_MODEL") or "MODEL_1").split("_")[-1]
    order = ([pref] if pref in nums else []) + [n for n in nums if n != pref]
    return [
        (
            env[f"MODEL_ENDPOINT_{n}"].rstrip("/"),
            f"MODEL_API_KEY_{n}",
            f"MODEL_NAME_{n}",
        )
        for n in order
    ]


_chat_cooling = {}
CHAT_COOLDOWN_S = 60.0


def default_chat(prompt, timeout=LLM_TIMEOUT_S):
    """The activity's transport: an OpenAI-style chat completion on the
    first configured MODEL_n that answers, MODEL_1 preferred. An endpoint
    that fails cools for CHAT_COOLDOWN_S so a dead mirror costs one
    timeout per minute, not one per turn. Keys are read here & travel only
    in the header. Returns the text, or raises the last failure."""
    import requests

    last = None
    for endpoint, key_env, name_env in chat_backends():
        if _chat_cooling.get(endpoint, 0.0) > time.monotonic():
            continue
        try:
            resp = requests.post(
                f"{endpoint}/chat/completions",
                headers={
                    "Authorization": f"Bearer {os.environ.get(key_env, '')}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": os.environ.get(name_env) or LLM_DEFAULT_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 300,
                    "temperature": 0.5,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:  # noqa: BLE001
            _chat_cooling[endpoint] = time.monotonic() + CHAT_COOLDOWN_S
            last = exc
    raise last or RuntimeError("no MODEL_ENDPOINT_n configured")


def parse_move(text, candidates, fired):
    """The cell named after MOVE:, else the first number in the text that
    is a candidate; None when the text names nothing usable."""
    if "MOVE:" in text:
        tail = text.split("MOVE:", 1)[1].strip()
        m = re.match(r"\D*(\d+)", tail)
        if m:
            cell = int(m.group(1))
            if cell in candidates and cell not in fired:
                return cell
    for n in re.findall(r"\b(\d+)\b", text):
        cell = int(n)
        if cell in candidates and cell not in fired:
            return cell
    return None


def llm_reasoner_shot(st, rng=random, chat=None):
    """Ask the chat model for a move among the grid's top cells. Records
    {ok, fallback, text} on st['llm_last'] so a run can say how often the
    model, not the grid, decided. Any failure fires the grid's own maximum."""
    grid, cands = llm_candidates(st)
    st["probability_matrix"] = [grid[r * SIZE : (r + 1) * SIZE] for r in range(SIZE)]
    candidates = [c for c, _ in cands]
    fired = set(st["shots"])
    chat = chat or default_chat
    text, ok = "", False
    if candidates:
        try:
            text = chat(llm_prompt(st, grid, cands))
            ok = True
        except Exception as exc:  # noqa: BLE001  the activity's own fail-open
            text = f"error: {type(exc).__name__}: {exc}"[:200]
    move = parse_move(text, candidates, fired) if ok else None
    fallback = move is None
    if fallback:
        move = jev_hunter.argmax_shot(grid, st["shots"], rng)
    st["llm_last"] = {"ok": ok, "fallback": fallback, "text": text[:400]}
    return move


# ── jev reasoner ───────────────────────────────────────────────────


def jev_reasoner_shot(st, rng=random, use_classifier=None):
    read = jev_hunter.choose_shot(
        st["shots"],
        st["hits"],
        st["sunk_cells"],
        remaining_sizes(st),
        rng=rng,
        use_classifier=use_classifier,
    )
    jev = read["jev"] or {}
    st["probability_matrix"] = [
        read["fused"][r * SIZE : (r + 1) * SIZE] for r in range(SIZE)
    ]
    st["jev_read"] = {
        "turn": len(st["shots"]) + 1,
        "shot": read["shot"],
        "source": read["source"],
        "error": read["error"],
        "candidates": read["candidates"],
        "p": jev.get("p"),
        "confidence": jev.get("confidence"),
        "orientation": jev.get("orientation"),
        "orientation_p": jev.get("orientation_p"),
        "instance": jev.get("instance"),
        "model": jev.get("model"),
        "fused": read["fused"],
        "sunk_cells": list(st["sunk_cells"]),
    }
    return read["shot"]


# ── dispatch ───────────────────────────────────────────────────────


def choose_shot(mode, st, rng=random, chat=None, use_classifier=None):
    """One shot for `mode` from state `st`. `chat` overrides the LLM
    transport (tests, an arena replay); `use_classifier` pins jev's gate."""
    if mode == "jev_reasoner":
        return jev_reasoner_shot(st, rng, use_classifier)
    if mode == "llm_reasoner":
        return llm_reasoner_shot(st, rng, chat)
    if mode == "super_hunter":
        return super_hunter_shot(st, rng)
    if mode == "hunter":
        return hunter_shot(st, rng)
    return random_shot(st, rng)


def record_shot(mode, st, shot, hit, sunk_ship=None, sunk_cells=()):
    """Fold the referee's verdict into the state: the shot, whether it hit,
    & (when it sank something) the ship's name & cells."""
    st["shots"].append(shot)
    if hit:
        st["hits"].append(shot)
    else:
        st["misses"].append(shot)
    if sunk_ship:
        st["sunk_ships"].append(sunk_ship)
        st["sunk_cells"].extend(c for c in sunk_cells if c not in st["sunk_cells"])
    return st
