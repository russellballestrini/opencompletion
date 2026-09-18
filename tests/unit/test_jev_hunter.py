"""jev_hunter.py: Jev Reasoner's probability grid & its one jev decision.

The exact placement-density grid (hunt mode, target mode, sunk cells,
boxed-in evidence), the candidate table jev reads, the wire of the one
classify call, the fusion, & the fail-open shot. Self-play proves the grid
sinks a random fleet well under 100 shots. No network: urlopen is replaced.
"""

import io
import json
import random
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import classifier  # noqa: E402
import jev_hunter as jh  # noqa: E402

ALL = [5, 4, 3, 3, 2]
ENV = {
    "MODEL_CLASSIFIER_ENDPOINT_0": "https://api.typesafe.ai/",
    "MODEL_CLASSIFIER_API_KEY_0": "apikey_test_never_printed",
    "OPENCOMPLETION_CLASSIFIER": "auto",
}


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen(bodies):
    seen = []
    queue = list(bodies)

    def fake(req, timeout=None):
        seen.append(req)
        b = queue.pop(0)
        if isinstance(b, Exception):
            raise b
        return _Resp(json.dumps(b).encode())

    fake.seen = seen
    return fake


def _answer(choice, probs, confidence=0.8, orientation=None):
    answers = {
        "answer": {
            "type": "choice",
            "choice": str(choice),
            "probabilities": {str(k): v for k, v in probs.items()},
            "confidence": confidence,
        }
    }
    if orientation:
        answers["orientation"] = {
            "type": "choice",
            "choice": orientation,
            "probabilities": {orientation: 0.9},
            "confidence": 0.9,
        }
    return {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 300}}


class TestDensityGrid(unittest.TestCase):
    def test_placements_count(self):
        # A ship of length L has 10 * (11 - L) placements per orientation.
        for size in (2, 3, 4, 5):
            self.assertEqual(len(jh.placements(size)), 2 * 10 * (11 - size))

    def test_empty_board_is_symmetric_and_peaks_at_centre(self):
        n = jh.normalize(jh.density_grid([], [], [], ALL))
        self.assertEqual(max(n), 100)
        self.assertEqual({c for c in range(100) if n[c] == 100}, {44, 45, 54, 55})
        self.assertEqual(n[0], n[9])
        self.assertEqual(n[0], n[90])
        self.assertEqual(n[0], n[99])
        self.assertLess(n[0], n[44])

    def test_misses_zero_themselves_and_starve_neighbours(self):
        before = jh.density_grid([], [], [], ALL)
        after = jh.density_grid([44], [], [], ALL)
        self.assertEqual(after[44], 0)
        self.assertLess(after[45], before[45])
        self.assertLess(after[34], before[34])
        self.assertEqual(after[99], before[99])  # far corner unaffected

    def test_live_hit_switches_to_target_mode(self):
        g = jh.density_grid([44], [44], [], ALL)
        self.assertEqual(g[44], 0)
        neighbours = {34, 43, 45, 54}
        self.assertEqual({c for c, _ in jh.top_candidates(g, [44], 4)}, neighbours)
        # Cells no placement through 44 can reach carry nothing.
        self.assertEqual(g[99], 0)
        self.assertEqual(g[0], 0)

    def test_two_live_hits_in_a_row_favour_the_line(self):
        g = jh.density_grid([44, 45], [44, 45], [], ALL)
        line = {43, 46}
        side = {34, 35, 54, 55}
        self.assertGreater(min(g[c] for c in line), max(g[c] for c in side))

    def test_sunk_cells_block_placements_and_leave_target_mode(self):
        # Destroyer at 44-45 sunk: its cells are blocked & no hit is live.
        g = jh.density_grid([44, 45], [44, 45], [44, 45], [5, 4, 3, 3])
        self.assertEqual(g[44], 0)
        self.assertEqual(g[45], 0)
        self.assertGreater(g[0], 0)  # hunt mode again: the far corner counts

    def test_boxed_in_live_hit_falls_back_to_hunt_mode(self):
        # A live hit at corner 0 with misses at 1 & 10: nothing can explain it.
        g = jh.density_grid([0, 1, 10], [0], [], ALL)
        self.assertTrue(any(g))
        self.assertEqual(g[0], 0)

    def test_normalize_and_top_candidates(self):
        self.assertEqual(jh.normalize([0, 0]), [0, 0])
        self.assertEqual(jh.normalize([2, 4]), [50, 100])
        grid = [0] * 100
        grid[7], grid[3], grid[50] = 9, 9, 1
        self.assertEqual(jh.top_candidates(grid, [], 2), [(3, 9), (7, 9)])
        self.assertEqual(jh.top_candidates(grid, [3], 2), [(7, 9), (50, 1)])
        self.assertEqual(jh.top_candidates([0] * 100, [], 3), [])

    def test_argmax_shot_random_among_ties_never_repeats(self):
        grid = [0] * 100
        grid[1] = grid[2] = 100
        rng = random.Random(3)
        picks = {jh.argmax_shot(grid, [], rng) for _ in range(30)}
        self.assertEqual(picks, {1, 2})
        self.assertEqual(jh.argmax_shot(grid, [1], rng), 2)
        self.assertIsNotNone(jh.argmax_shot([0] * 100, [1, 2], rng))
        self.assertIsNone(jh.argmax_shot(grid, list(range(100)), rng))


class TestWhatJevReads(unittest.TestCase):
    def test_board_ascii(self):
        text = jh.board_ascii([1, 44, 55], [44, 55], [55])
        rows = text.splitlines()
        self.assertEqual(len(rows), 11)
        self.assertEqual(rows[1].split()[2], "o")
        self.assertEqual(rows[5].split()[5], "X")
        self.assertEqual(rows[6].split()[6], "#")

    def test_describe_candidate_names_the_evidence(self):
        d = jh.describe_candidate(46, 100, [44, 45], [])
        self.assertIn("Cell 46 (row 4, column 6)", d)
        self.assertIn("density 100/100", d)
        self.assertIn("touches a live hit left", d)
        self.assertIn("extends a horizontal line of 2 live hits", d)
        d = jh.describe_candidate(34, 80, [44], [])
        self.assertIn("touches a live hit below", d)
        self.assertIn("vertical pair", d)
        self.assertNotIn("touches", jh.describe_candidate(0, 10, [44], []))
        # A sunk hit is no longer live evidence.
        self.assertNotIn("touches", jh.describe_candidate(46, 10, [45], [45]))

    def test_line_of_live_does_not_wrap_rows(self):
        # 49 & 50 are adjacent numbers on different rows: no horizontal line.
        self.assertEqual(jh._line_of_live(50, {49}), (0, None))
        self.assertEqual(jh._line_of_live(50, {40, 30}), (2, "vertical"))


class TestJevWire(unittest.TestCase):
    def setUp(self):
        classifier._reset_cooling()
        classifier.reset_stats()
        self._env = mock.patch.dict("os.environ", ENV)
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_one_call_choice_over_candidates_plus_orientation(self):
        grid = jh.normalize(jh.density_grid([44], [44], [], ALL))
        fake = _urlopen(
            [
                _answer(
                    45, {45: 0.6, 43: 0.2, 34: 0.1, 54: 0.1}, orientation="horizontal"
                )
            ]
        )
        with mock.patch("urllib.request.urlopen", fake):
            r = jh.jev_read(grid, [44], [44], [], ALL)
        self.assertEqual(len(fake.seen), 1)
        body = json.loads(fake.seen[0].data)
        self.assertIn("X", body["state"])
        self.assertIn("Live hits not yet sunk: [44]", body["state"])
        q = body["questions"]["answer"]
        self.assertEqual(set(q["criteria"]), {"34", "43", "45", "54", "24", "42"})
        self.assertIn("touches a live hit left", q["criteria"]["45"])
        self.assertEqual(
            set(body["questions"]["orientation"]["criteria"]),
            {"horizontal", "vertical", "unknown"},
        )
        self.assertEqual(r["value"], 45)
        self.assertEqual(r["p"], 0.6)
        self.assertEqual(r["orientation"], "horizontal")
        self.assertEqual(r["orientation_p"], 0.9)
        self.assertEqual(r["instance"], "typesafe")
        self.assertEqual(sorted(r["candidates"]), [24, 34, 42, 43, 45, 54])

    def test_no_live_hit_asks_no_orientation(self):
        grid = jh.normalize(jh.density_grid([], [], [], ALL))
        fake = _urlopen([_answer(44, {44: 1.0})])
        with mock.patch("urllib.request.urlopen", fake):
            r = jh.jev_read(grid, [], [], [], ALL)
        self.assertNotIn("orientation", json.loads(fake.seen[0].data)["questions"])
        self.assertIsNone(r["orientation"])

    def test_no_candidates_is_an_error(self):
        with self.assertRaises(classifier.ClassifierError):
            jh.jev_read([0] * 100, [], [], [], ALL)


class TestFuseAndChoose(unittest.TestCase):
    def setUp(self):
        classifier._reset_cooling()
        self._env = mock.patch.dict("os.environ", ENV)
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_fuse_lets_jev_break_a_tie_but_not_a_landslide(self):
        grid = [0] * 100
        grid[1] = grid[2] = 100
        fused = jh.fuse(grid, {2: 1.0})
        self.assertEqual(fused[2], 100)
        self.assertLess(fused[1], 100)
        # A near-tie: jev's favourite at 60% of the top density wins.
        grid = [0] * 100
        grid[1], grid[2] = 100, 60
        self.assertGreater(jh.fuse(grid, {2: 1.0})[2], jh.fuse(grid, {2: 1.0})[1])
        # A landslide: jev's whole mass on a density-1 cell loses to 100,
        # however many cells share the top density.
        grid = [0] * 100
        grid[1], grid[2] = 100, 1
        grid[3:50] = [100] * 47
        fused = jh.fuse(grid, {2: 1.0})
        self.assertLess(fused[2], fused[1])
        # Jev spreading its mass still gives its favourite the full weight.
        grid = [0] * 100
        grid[1], grid[2] = 100, 60
        self.assertGreater(jh.fuse(grid, {2: 0.5, 1: 0.1, 5: 0.4})[2], 100 - 1)
        self.assertEqual(jh.fuse([0] * 100, {5: 1.0}), [0] * 100)

    def test_choose_shot_grid_only_when_pinned_off(self):
        r = jh.choose_shot([44], [44], [], ALL, use_classifier=False)
        self.assertEqual(r["source"], "grid")
        self.assertIsNone(r["jev"])
        self.assertIsNone(r["error"])
        self.assertIn(r["shot"], {34, 43, 45, 54})
        self.assertEqual(r["fused"], r["grid"])

    def test_choose_shot_fails_open_on_vendor_error(self):
        fake = _urlopen([TimeoutError("slow")])
        with mock.patch("urllib.request.urlopen", fake):
            r = jh.choose_shot([44], [44], [], ALL)
        self.assertEqual(r["source"], "grid")
        self.assertIn("TimeoutError", r["error"])
        self.assertIn(r["shot"], {34, 43, 45, 54})

    def test_choose_shot_fuses_jev_and_jev_breaks_the_tie(self):
        fake = _urlopen([_answer(54, {54: 0.9, 34: 0.1}, orientation="vertical")])
        with mock.patch("urllib.request.urlopen", fake):
            r = jh.choose_shot([44], [44], [], ALL)
        self.assertEqual(r["source"], "jev")
        self.assertEqual(r["shot"], 54)
        self.assertEqual(r["fused"][54], 100)
        self.assertEqual(r["jev"]["orientation"], "vertical")
        self.assertEqual(len(r["candidates"]), jh.CANDIDATES)

    def test_knob_off_never_calls(self):
        fake = _urlopen([_answer(54, {54: 1.0})])
        with mock.patch.dict(
            "os.environ", {"OPENCOMPLETION_CLASSIFIER": "off"}
        ), mock.patch("urllib.request.urlopen", fake):
            r = jh.choose_shot([], [], [], ALL)
        self.assertEqual(fake.seen, [])
        self.assertEqual(r["source"], "grid")


class TestSelfPlay(unittest.TestCase):
    def test_place_ships_is_a_legal_fleet(self):
        board = jh.place_ships(random.Random(7))
        counts = {}
        for s in board:
            if s != -1:
                counts[s] = counts.get(s, 0) + 1
        self.assertEqual(counts, jh.SHIPS)

    def test_grid_sinks_a_fleet_well_under_a_full_board(self):
        rng = random.Random(11)
        shots = [jh.play(jh.place_ships(rng), rng) for _ in range(20)]
        self.assertLessEqual(max(shots), 100)
        self.assertLess(sum(shots) / len(shots), 60)
        r = jh.benchmark(games=5, seed=2)
        self.assertEqual(r["games"], 5)
        self.assertLessEqual(r["min"], r["median"])
        self.assertLessEqual(r["median"], r["max"])

    def test_draw_heatmap_renders(self):
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots()
        fused = jh.normalize(jh.density_grid([1, 44], [44], [], ALL))
        jh.draw_heatmap(ax, fused, [1, 44, 45], [44, 45], [45], shot=43)
        self.assertEqual(ax.get_title(), "Jev Reasoner's Read")
        plt.close(fig)


if __name__ == "__main__":
    unittest.main()
