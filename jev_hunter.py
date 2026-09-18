"""Jev Reasoner: a probability grid for Battleship, read by a decision model.

Our Battleship activity (`research/activity29-battleship.yaml`) pits a
player against one of several targeting algorithms. Super Human Hunter
keeps a heuristic grid: seed with placement counts, bump neighbours of a
hit, zero a miss. LLM Reasoner hands a chat model a list of candidates &
parses a number back out of prose. Jev Reasoner does both jobs properly:

1. **An exact placement-density grid.** Every turn, for every ship still
   afloat, enumerate every horizontal & vertical placement that avoids
   every miss & every cell of a sunk ship. Each surviving placement adds
   weight to the cells it covers. While a hit is live (struck, not yet
   sunk), only placements that explain a live hit count, weighted by how
   many live hits they cover, so the grid chases & completes a wounded
   ship before hunting fresh water. Normalised to 100 at the maximum, the
   convention fox's rubric names.

2. **One jev decision per turn.** The top candidates become the labels of
   ONE `choice` question to a classifier model (`classifier.py`: TypeSafe
   System One, model `jev-latest`, or any decision endpoint sharing that
   wire shape). The state is the ASCII board & the evidence per candidate;
   the answer is a probability per candidate & a confidence, with a second
   `choice` on which way the wounded ship lies. That distribution is fused
   with the density grid (`JEV_WEIGHT`), & the fused grid's maximum is
   the shot.

Fail-open: no classifier configured, a timeout, an error, & the density
grid alone decides, exactly as Super Human Hunter would with a correct
grid. The readout says which happened (`source: jev | grid`), so a game
transcript never hides a vendor that was quietly off.

Cells are 0..99, row-major: cell = row * 10 + col, row 0 at the top.
Ship names & sizes follow the activity: Carrier 5, Battleship 4,
Cruiser 3, Submarine 3, Destroyer 2. Pure functions over plain lists &
sets, so the activity's `processing_script` stores everything in room
metadata & a unit test never needs a network.

    python3 jev_hunter.py [games] [seed]   # self-play benchmark, grid only
"""

import random
import sys

import classifier

SIZE = 10
CELLS = SIZE * SIZE
SHIPS = {
    "Carrier": 5,
    "Battleship": 4,
    "Cruiser": 3,
    "Submarine": 3,
    "Destroyer": 2,
}
# How many top-density cells jev is asked to choose between. Six keeps the
# question small (a few hundred input tokens) while covering every cell
# that is ever a serious contender on a 10x10 board.
CANDIDATES = 6
# Share of the fused grid that jev's favourite earns; the rest stays with
# the exact density, each on its own max scale. 0.4: jev overturns any
# candidate above ~a third of the top density, never a landslide.
JEV_WEIGHT = 0.4

_placements = {}


def placements(size):
    """Every horizontal & vertical placement of a ship of `size`, as
    tuples of cells. Memoised: 10x10 has only a few hundred per size."""
    if size not in _placements:
        out = []
        for row in range(SIZE):
            for col in range(SIZE - size + 1):
                out.append(tuple(row * SIZE + col + i for i in range(size)))
        for row in range(SIZE - size + 1):
            for col in range(SIZE):
                out.append(tuple((row + i) * SIZE + col for i in range(size)))
        _placements[size] = out
    return _placements[size]


def density_grid(shots, hits, sunk_cells, remaining_sizes):
    """Exact placement density: for every ship still afloat, count every
    placement consistent with the evidence, weighting the cells it covers.
    A placement may not cross a miss or a sunk ship's cell. While live
    (unsunk) hits exist, only placements covering a live hit count, weighted
    by how many they cover: target mode. Returns a list of 100 non-negative
    ints; fired cells are 0."""
    shots = set(shots)
    hits = set(hits)
    sunk = set(sunk_cells)
    misses = shots - hits
    live = hits - sunk
    blocked = misses | sunk

    def count(target):
        grid = [0] * CELLS
        for size in remaining_sizes:
            for cells in placements(size):
                if any(c in blocked for c in cells):
                    continue
                covered = sum(1 for c in cells if c in live)
                if target and covered == 0:
                    continue
                weight = covered if target else 1
                for c in cells:
                    if c not in shots:
                        grid[c] += weight
        return grid

    grid = count(bool(live))
    if live and not any(grid):
        # Evidence no placement explains (a live hit boxed in): hunt mode.
        grid = count(False)
    return grid


def normalize(grid):
    """Scale a grid so its maximum reads 100 (ints). All-zero stays zero."""
    top = max(grid) if grid else 0
    if top <= 0:
        return [0] * len(grid)
    return [round(v * 100 / top) for v in grid]


def top_candidates(grid, shots, k=CANDIDATES):
    """The `k` highest-density unfired cells as [(cell, value)], value
    descending, cell ascending on ties. Cells at zero are never offered."""
    shots = set(shots)
    ranked = sorted(
        ((c, v) for c, v in enumerate(grid) if v > 0 and c not in shots),
        key=lambda cv: (-cv[1], cv[0]),
    )
    return ranked[:k]


def argmax_shot(grid, shots, rng=random):
    """An unfired cell of maximal value, chosen at random among ties; any
    unfired cell when the grid is flat zero."""
    shots = set(shots)
    free = [c for c in range(CELLS) if c not in shots]
    if not free:
        return None
    top = max(grid[c] for c in free)
    return rng.choice([c for c in free if grid[c] == top])


# ── What jev reads ─────────────────────────────────────────────────


def board_ascii(shots, hits, sunk_cells):
    """The board as jev sees it: `.` unknown, `o` miss, `X` live hit,
    `#` sunk, with row & column labels."""
    shots, hits, sunk = set(shots), set(hits), set(sunk_cells)
    lines = ["   " + " ".join(str(c) for c in range(SIZE))]
    for row in range(SIZE):
        cells = []
        for col in range(SIZE):
            c = row * SIZE + col
            if c in sunk:
                cells.append("#")
            elif c in hits:
                cells.append("X")
            elif c in shots:
                cells.append("o")
            else:
                cells.append(".")
        lines.append(f"{row:>2} " + " ".join(cells))
    return "\n".join(lines)


def _neighbours(cell):
    row, col = divmod(cell, SIZE)
    out = {}
    if row > 0:
        out["above"] = cell - SIZE
    if row < SIZE - 1:
        out["below"] = cell + SIZE
    if col > 0:
        out["left"] = cell - 1
    if col < SIZE - 1:
        out["right"] = cell + 1
    return out


def _line_of_live(cell, live):
    """The longest straight run of live hits a cell would extend, as
    (count, 'horizontal' | 'vertical'), or (0, None)."""
    best = (0, None)
    for axis, step in (("horizontal", 1), ("vertical", SIZE)):
        run = 0
        for direction in (-step, step):
            c = cell + direction
            while c in live and 0 <= c < CELLS:
                if step == 1 and c // SIZE != cell // SIZE:
                    break
                run += 1
                c += direction
        if run > best[0]:
            best = (run, axis)
    return best


def describe_candidate(cell, value, hits, sunk_cells):
    """One criterion line for jev: where the cell is, its density, & what
    live evidence it touches."""
    live = set(hits) - set(sunk_cells)
    row, col = divmod(cell, SIZE)
    parts = [f"Cell {cell} (row {row}, column {col}): density {value}/100"]
    touching = [name for name, n in _neighbours(cell).items() if n in live]
    if touching:
        parts.append("touches a live hit " + " & ".join(touching))
    run, axis = _line_of_live(cell, live)
    if run >= 2:
        parts.append(f"extends a {axis} line of {run} live hits")
    elif run == 1:
        parts.append(f"would form a {axis} pair with a live hit")
    return "; ".join(parts) + "."


def jev_read(grid, shots, hits, sunk_cells, remaining_sizes, *, k=CANDIDATES, **kw):
    """ONE classify call: a `choice` over the top-`k` candidates (their
    density & evidence as criteria) &, while a hit is live, a `choice` on
    the wounded ship's orientation. Returns {candidates, value, p, dist,
    confidence, orientation, orientation_p, instance, model} or raises
    classifier.ClassifierError. `kw` reaches `classifier.choose` (model,
    timeout)."""
    cands = top_candidates(grid, shots, k)
    if not cands:
        raise classifier.ClassifierError("no candidates to choose between")
    live = set(hits) - set(sunk_cells)
    labels = [str(c) for c, _ in cands]
    criteria = {str(c): describe_candidate(c, v, hits, sunk_cells) for c, v in cands}
    afloat = ", ".join(str(s) for s in sorted(remaining_sizes, reverse=True))
    state = (
        f"Battleship, 10x10, cells 0-99 (cell = row*10 + column). "
        f"Turn {len(set(shots)) + 1}. Ships still afloat (lengths): {afloat}. "
        f"Live hits not yet sunk: {sorted(live) or 'none'}.\n"
        "Board (. unknown, o miss, X live hit, # sunk):\n"
        + board_ascii(shots, hits, sunk_cells)
    )
    extra = {}
    if live:
        extra["orientation"] = {
            "type": "choice",
            "instructions": "The live hits belong to a ship lying which way?",
            "criteria": {
                "horizontal": "The wounded ship runs left-right along a row.",
                "vertical": "The wounded ship runs up-down along a column.",
                "unknown": "Not enough evidence to tell yet.",
            },
        }
    d = classifier.choose(
        state,
        labels,
        instructions=(
            "You are Jev, admiral of a Battleship fleet. Choose the cell to fire "
            "on next that sinks the enemy fleet in the fewest shots: finish a "
            "wounded ship along its line before hunting open water; in open "
            "water prefer cells more ship placements can cover."
        ),
        criteria=criteria,
        extra=extra,
        **kw,
    )
    orient = (d.get("extra") or {}).get("orientation") or {}
    return dict(
        candidates=[c for c, _ in cands],
        value=int(d["value"]),
        p=d["p"],
        dist={int(c): p for c, p in d["dist"].items()},
        confidence=d["confidence"],
        orientation=orient.get("choice"),
        orientation_p=classifier._round(orient.get("confidence")),
        instance=d["instance"],
        model=d["model"],
    )


def fuse(grid, jev_dist, weight=JEV_WEIGHT):
    """Blend a density grid with jev's distribution over candidate cells.
    Each side is scaled to its own maximum (the grid to 1 at its densest
    cell, jev to 1 on its favourite), mixed `1 - weight` : `weight`, then
    normalised back to 100 at the maximum. On that scale jev's favourite
    gains exactly `weight`, so it overturns a near-tie (a candidate above
    roughly `1 - 2 * weight` of the top density) & never a landslide."""
    top = max(grid) if grid else 0
    if top <= 0:
        return normalize(grid)
    jev_top = max(jev_dist.values()) if jev_dist else 0
    fused = [(1 - weight) * v / top for v in grid]
    if jev_top > 0:
        for cell, p in jev_dist.items():
            if 0 <= cell < CELLS:
                fused[cell] += weight * p / jev_top
    peak = max(fused)
    return [round(v * 100 / peak) for v in fused] if peak > 0 else [0] * CELLS


def choose_shot(
    shots,
    hits,
    sunk_cells,
    remaining_sizes,
    *,
    rng=random,
    use_classifier=None,
    k=CANDIDATES,
):
    """Jev Reasoner's shot for this turn. Returns a dict the activity
    stores as `jev_read`:

        shot         the cell to fire on
        source       'jev' (fused with a classifier answer) or 'grid'
        grid         density grid, normalised to 100
        fused        the grid that decided (== grid when source is 'grid')
        candidates   top cells offered to jev, [(cell, density)]
        jev          the `jev_read` answer, or None
        error        why jev did not answer, or None

    `use_classifier` pins the gate (tests, benchmarks); None consults
    `classifier.available()`."""
    grid = density_grid(shots, hits, sunk_cells, remaining_sizes)
    norm = normalize(grid)
    cands = top_candidates(norm, shots, k)
    jev = error = None
    gate = classifier.available() if use_classifier is None else use_classifier
    if gate and cands:
        try:
            jev = jev_read(norm, shots, hits, sunk_cells, remaining_sizes, k=k)
        except classifier.ClassifierError as exc:
            error = str(exc)[:200]
        except Exception as exc:  # noqa: BLE001  a defect here, not the vendor
            error = f"{type(exc).__name__}: {exc}"[:200]
    fused = fuse(norm, jev["dist"]) if jev else norm
    shot = argmax_shot(fused, shots, rng)
    return dict(
        shot=shot,
        source="jev" if jev else "grid",
        grid=norm,
        fused=fused,
        candidates=cands,
        jev=jev,
        error=error,
    )


# ── Rendering ──────────────────────────────────────────────────────


def draw_heatmap(
    ax, fused, shots, hits, sunk_cells, shot=None, title="Jev Reasoner's Read"
):
    """A 10x10 heatmap of the fused grid on a matplotlib axis, row 0 at the
    top to match the activity's boards: misses dotted, hits crossed, sunk
    cells hatched, the chosen shot starred."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: F401  Agg backend must be set first

    shots, hits, sunk = set(shots), set(hits), set(sunk_cells)
    rows = [fused[r * SIZE : (r + 1) * SIZE] for r in range(SIZE)]
    ax.imshow(rows, cmap="inferno", vmin=0, vmax=100, origin="upper")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(title, fontsize=12)
    for c in range(CELLS):
        row, col = divmod(c, SIZE)
        if c in sunk:
            ax.text(col, row, "#", ha="center", va="center", color="cyan", fontsize=10)
        elif c in hits:
            ax.text(col, row, "X", ha="center", va="center", color="red", fontsize=12)
        elif c in shots:
            ax.text(col, row, "o", ha="center", va="center", color="white", fontsize=9)
        elif fused[c] >= 50:
            ax.text(
                col,
                row,
                str(fused[c]),
                ha="center",
                va="center",
                color="black",
                fontsize=7,
            )
    if shot is not None:
        row, col = divmod(shot, SIZE)
        ax.text(col, row, "★", ha="center", va="center", color="lime", fontsize=16)


# ── Self-play ──────────────────────────────────────────────────────


def place_ships(rng=random, ships=SHIPS):
    """A random non-overlapping fleet: a list of 100 entries, ship name or
    -1, the activity's board shape."""
    board = [-1] * CELLS
    for name, size in ships.items():
        while True:
            cells = rng.choice(placements(size))
            if all(board[c] == -1 for c in cells):
                for c in cells:
                    board[c] = name
                break
    return board


def play(board, rng=random, use_classifier=False, ships=SHIPS):
    """Sink every ship on `board`; returns the number of shots taken."""
    shots, hits, sunk_cells, sunk = [], [], set(), set()
    while len(sunk) < len(ships):
        remaining = [s for n, s in ships.items() if n not in sunk]
        r = choose_shot(
            shots, hits, sunk_cells, remaining, rng=rng, use_classifier=use_classifier
        )
        shot = r["shot"]
        shots.append(shot)
        if board[shot] != -1:
            hits.append(shot)
            name = board[shot]
            cells = [c for c, s in enumerate(board) if s == name]
            if all(c in hits for c in cells):
                sunk.add(name)
                sunk_cells.update(cells)
    return len(shots)


def benchmark(games=200, seed=0, use_classifier=False):
    """Mean, median, min & max shots to sink a random fleet over `games`
    boards. Grid only unless `use_classifier`."""
    rng = random.Random(seed)
    results = sorted(play(place_ships(rng), rng, use_classifier) for _ in range(games))
    return dict(
        games=games,
        mean=round(sum(results) / len(results), 2),
        median=results[len(results) // 2],
        min=results[0],
        max=results[-1],
    )


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    s = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    r = benchmark(n, s)
    print(
        f"jev_hunter grid-only self-play: {r['games']} games, seed {s}: "
        f"mean {r['mean']} shots, median {r['median']}, min {r['min']}, max {r['max']}"
    )
