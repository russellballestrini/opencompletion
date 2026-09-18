"""battleship_modes.py & battleship_arena.py: the five admirals & the
round robin that measures them. No network: the chat transport is
injected & the classifier knob is pinned off."""

import json
import random
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import battleship_arena as arena  # noqa: E402
import battleship_modes as bm  # noqa: E402
import jev_hunter  # noqa: E402


def _state(shots=(), hits=(), sunk_ships=(), sunk_cells=()):
    st = bm.new_state()
    st["shots"] = list(shots)
    st["hits"] = list(hits)
    st["misses"] = [s for s in shots if s not in hits]
    st["sunk_ships"] = list(sunk_ships)
    st["sunk_cells"] = list(sunk_cells)
    return st


class TestRandom(unittest.TestCase):
    def test_random_never_repeats(self):
        st = _state(shots=range(99))
        self.assertEqual(bm.random_shot(st, random.Random(1)), 99)


class TestHunter(unittest.TestCase):
    def test_hunts_on_a_checkerboard_while_the_destroyer_lives(self):
        st = _state()
        picks = {bm.hunter_shot(st, random.Random(i)) for i in range(60)}
        self.assertTrue(all((c // 10 + c % 10) % 2 == 0 for c in picks))
        # Destroyer sunk: parity widens to the Cruiser's 3.
        st = _state(sunk_ships=["Destroyer"])
        picks = {bm.hunter_shot(st, random.Random(i)) for i in range(60)}
        self.assertTrue(all((c // 10 + c % 10) % 3 == 0 for c in picks))

    def test_parity_exhausted_falls_back_to_any_cell(self):
        st = _state(shots=[c for c in range(100) if (c // 10 + c % 10) % 2 == 0])
        self.assertNotIn(bm.hunter_shot(st, random.Random(0)), st["shots"])

    def test_targets_a_neighbour_of_a_live_hit(self):
        st = _state(shots=[44], hits=[44])
        picks = {bm.hunter_shot(st, random.Random(i)) for i in range(20)}
        self.assertEqual(picks, {34, 43, 45, 54})

    def test_keeps_targeting_after_a_miss(self):
        st = _state(shots=[44, 45], hits=[44])
        self.assertIn(bm.hunter_shot(st, random.Random(3)), {34, 43, 54})

    def test_extends_a_line_of_hits_at_both_ends(self):
        st = _state(shots=[44, 45], hits=[44, 45])
        self.assertEqual(bm.line_targets([44, 45], set(st["shots"])), [43, 46])
        picks = {bm.hunter_shot(st, random.Random(i)) for i in range(20)}
        self.assertEqual(picks, {43, 46})
        # Vertical, one end blocked by a miss.
        st = _state(shots=[24, 34, 44, 54], hits=[34, 44])
        self.assertEqual(bm.line_targets([34, 44], set(st["shots"])), [])
        picks = {bm.hunter_shot(st, random.Random(i)) for i in range(20)}
        self.assertTrue(picks <= {33, 35, 43, 45})

    def test_lines_never_wrap_rows(self):
        self.assertEqual(bm.line_targets([49, 50], set()), [])
        self.assertEqual(bm.line_targets([9, 19], set()), [29])

    def test_sunk_hits_are_not_live(self):
        st = _state(
            shots=[44, 45], hits=[44, 45], sunk_ships=["Destroyer"], sunk_cells=[44, 45]
        )
        self.assertEqual(bm.live_hits(st), [])
        c = bm.hunter_shot(st, random.Random(0))
        self.assertNotIn(c, {34, 35, 43, 46, 54, 55})


class TestSuperHunter(unittest.TestCase):
    def test_is_the_exact_density_grid(self):
        st = _state(shots=[44], hits=[44])
        picks = {bm.super_hunter_shot(st, random.Random(i)) for i in range(20)}
        self.assertEqual(picks, {34, 43, 45, 54})
        self.assertEqual(len(st["probability_matrix"]), 10)
        self.assertEqual(max(max(r) for r in st["probability_matrix"]), 100)

    def test_opening_shot_is_a_centre_cell(self):
        st = _state()
        picks = {bm.super_hunter_shot(st, random.Random(i)) for i in range(30)}
        self.assertEqual(picks, {44, 45, 54, 55})

    def test_never_calls_a_classifier(self):
        env = {
            "MODEL_CLASSIFIER_ENDPOINT_0": "https://api.typesafe.ai",
            "OPENCOMPLETION_CLASSIFIER": "on",
        }
        with mock.patch.dict("os.environ", env), mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("network")
        ):
            bm.super_hunter_shot(_state(), random.Random(0))


class TestLLMReasoner(unittest.TestCase):
    def test_candidates_come_from_the_exact_grid(self):
        st = _state(shots=[44], hits=[44])
        grid, cands = bm.llm_candidates(st)
        self.assertEqual(grid[44], 0)
        self.assertEqual({c for c, v in cands if v == 100}, {34, 43, 45, 54})
        self.assertEqual(len(cands), bm.LLM_CANDIDATES)

    def test_prompt_carries_board_and_evidence(self):
        st = _state(shots=[44, 45], hits=[44, 45])
        text = bm.llm_prompt(st, *bm.llm_candidates(st))
        self.assertIn("Turn 3", text)
        self.assertIn("X X", text)
        self.assertIn("extends a horizontal line of 2 live hits", text)
        self.assertIn("MOVE:", text)

    def test_parse_move(self):
        cands = [45, 43, 34, 54, 24, 42]
        self.assertEqual(bm.parse_move("ANALYSIS: x\n\nMOVE: 43", cands, set()), 43)
        self.assertEqual(bm.parse_move("MOVE: [45]", cands, set()), 45)
        self.assertEqual(bm.parse_move("I choose 34 because", cands, set()), 34)
        self.assertIsNone(bm.parse_move("MOVE: 43", cands, {43}))
        self.assertIsNone(bm.parse_move("MOVE: 99", cands, set()))
        self.assertIsNone(bm.parse_move("no numbers here", cands, set()))

    def test_chat_answer_is_used_and_recorded(self):
        st = _state(shots=[44], hits=[44])
        seen = []

        def chat(prompt):
            seen.append(prompt)
            return "ANALYSIS: a b c\n\nMOVE: 54"

        self.assertEqual(bm.llm_reasoner_shot(st, random.Random(0), chat), 54)
        self.assertIn("Turn 2", seen[0])
        self.assertEqual(
            st["llm_last"],
            {"ok": True, "fallback": False, "text": "ANALYSIS: a b c\n\nMOVE: 54"},
        )
        self.assertEqual(len(st["probability_matrix"]), 10)

    def test_dead_endpoint_fires_the_grid_maximum(self):
        st = _state(shots=[44], hits=[44])

        def chat(prompt):
            raise ConnectionError("down")

        move = bm.llm_reasoner_shot(st, random.Random(0), chat)
        self.assertIn(move, {34, 43, 45, 54})
        self.assertFalse(st["llm_last"]["ok"])
        self.assertTrue(st["llm_last"]["fallback"])
        self.assertIn("ConnectionError", st["llm_last"]["text"])

    def test_unusable_answer_falls_back(self):
        st = _state()
        move = bm.llm_reasoner_shot(st, random.Random(0), lambda p: "MOVE: 999")
        self.assertIn(move, {44, 45, 54, 55})
        self.assertTrue(st["llm_last"]["ok"])
        self.assertTrue(st["llm_last"]["fallback"])


class TestChatTransport(unittest.TestCase):
    ENV = {
        "MODEL_ENDPOINT_1": "https://one.test/v1",
        "MODEL_API_KEY_1": "k1",
        "MODEL_ENDPOINT_2": "https://two.test/v1/",
        "MODEL_API_KEY_2": "k2",
        "MODEL_NAME_2": "qwen",
    }

    def test_backends_prefer_model_1_then_ascend(self):
        self.assertEqual(
            [b[0] for b in bm.chat_backends(self.ENV)],
            ["https://one.test/v1", "https://two.test/v1"],
        )
        env = dict(self.ENV, LLM_MODEL="MODEL_2")
        self.assertEqual(bm.chat_backends(env)[0][0], "https://two.test/v1")

    def test_unnamed_endpoint_uses_its_first_listed_model(self):
        bm._chat_models.clear()
        calls = []

        def get(url, headers=None, timeout=None):
            calls.append(url)
            m = mock.MagicMock()
            m.raise_for_status.return_value = None
            m.json.return_value = {"data": [{"id": "served-model"}]}
            return m

        env = {"MODEL_ENDPOINT_1": "https://one.test/v1", "MODEL_API_KEY_1": "k"}
        with mock.patch.dict("os.environ", env, clear=False), mock.patch(
            "requests.get", get
        ):
            self.assertEqual(
                bm._model_for("https://one.test/v1", "MODEL_NAME_1", {}, 5),
                "served-model",
            )
            # Cached: a second ask costs no request.
            bm._model_for("https://one.test/v1", "MODEL_NAME_1", {}, 5)
        self.assertEqual(calls, ["https://one.test/v1/models"])
        with mock.patch.dict("os.environ", {"MODEL_NAME_1": "pinned"}):
            self.assertEqual(
                bm._model_for("https://one.test/v1", "MODEL_NAME_1", {}, 5), "pinned"
            )
        bm._chat_models.clear()

    def test_dead_first_endpoint_falls_through_and_cools(self):
        bm._chat_cooling.clear()
        calls = []

        def post(url, headers=None, json=None, timeout=None):
            calls.append((url, json["model"]))
            if "one.test" in url:
                raise ConnectionError("502")
            m = mock.MagicMock()
            m.raise_for_status.return_value = None
            m.json.return_value = {"choices": [{"message": {"content": "MOVE: 44"}}]}
            return m

        with mock.patch.dict("os.environ", self.ENV), mock.patch(
            "requests.post", post
        ), mock.patch("requests.get", side_effect=ConnectionError("502")):
            self.assertEqual(bm.default_chat("p"), "MOVE: 44")
            self.assertEqual(bm.default_chat("q"), "MOVE: 44")
        self.assertEqual(
            [c[0] for c in calls],
            [
                "https://one.test/v1/chat/completions",
                "https://two.test/v1/chat/completions",
                "https://two.test/v1/chat/completions",
            ],
        )
        self.assertEqual(calls[1][1], "qwen")
        self.assertIn("https://one.test/v1", bm._chat_cooling)
        bm._chat_cooling.clear()


class TestJevAndDispatch(unittest.TestCase):
    def test_jev_reasoner_fills_readout_and_matrix(self):
        st = _state(shots=[44], hits=[44])
        shot = bm.choose_shot(
            "jev_reasoner", st, random.Random(0), use_classifier=False
        )
        self.assertIn(shot, {34, 43, 45, 54})
        self.assertEqual(st["jev_read"]["source"], "grid")
        self.assertEqual(st["jev_read"]["shot"], shot)
        self.assertEqual(len(st["probability_matrix"]), 10)

    def test_record_shot_tracks_sinking(self):
        st = _state()
        bm.record_shot("hunter", st, 44, True, "Destroyer", (44, 45))
        self.assertEqual(st["sunk_ships"], ["Destroyer"])
        self.assertEqual(st["sunk_cells"], [44, 45])
        self.assertEqual(bm.remaining_sizes(st), [5, 4, 3, 3])
        bm.record_shot("hunter", st, 46, False)
        self.assertEqual(st["misses"], [46])

    def test_every_mode_dispatches(self):
        for mode in bm.MODES:
            st = _state()
            shot = bm.choose_shot(
                mode,
                st,
                random.Random(0),
                chat=lambda p: "MOVE: 44",
                use_classifier=False,
            )
            self.assertTrue(0 <= shot < 100, mode)

    def test_state_is_json_safe(self):
        st = _state()
        bm.choose_shot("jev_reasoner", st, random.Random(0), use_classifier=False)
        json.dumps(st)


class TestArena(unittest.TestCase):
    def test_judge(self):
        board = jev_hunter.place_ships(random.Random(5))
        cells = [c for c, s in enumerate(board) if s == "Destroyer"]
        self.assertEqual(arena.judge(board, cells[0], {cells[0]}), (True, None, ()))
        self.assertEqual(
            arena.judge(board, cells[1], set(cells)), (True, "Destroyer", tuple(cells))
        )
        empty = board.index(-1)
        self.assertEqual(arena.judge(board, empty, set()), (False, None, ()))

    def test_game_is_seeded_and_ends_with_a_winner(self):
        a = arena.play_game("super_hunter", "random", "k:1", "a", use_classifier=False)
        b = arena.play_game("super_hunter", "random", "k:1", "a", use_classifier=False)
        strip = lambda r: {k: v for k, v in r.items() if k != "seconds"}  # noqa: E731
        self.assertEqual(strip(a), strip(b))
        self.assertIn(a["winner"], ("a", "b"))
        self.assertLessEqual(max(a["a_shots"], a["b_shots"]), 100)
        self.assertEqual(
            a["winner_mode"], "super_hunter" if a["winner"] == "a" else "random"
        )

    def test_schedule_alternates_first_mover(self):
        games, solos = arena.schedule(["random", "hunter", "super_hunter"], 4, 0)
        self.assertEqual(len(games), 12)
        self.assertEqual([g["first"] for g in games[:4]], ["a", "b", "a", "b"])
        self.assertEqual(len(solos), 3 * 8)

    def test_round_robin_summary_and_report(self):
        with mock.patch.dict("os.environ", {"OPENCOMPLETION_CLASSIFIER": "off"}):
            r = arena.run(
                modes=["random", "super_hunter", "llm_reasoner"],
                games_per_pair=2,
                seed=3,
                workers=2,
                chat=lambda p: "MOVE: 44",
            )
        s = r["summary"]
        self.assertEqual(len(r["games"]), 6)
        self.assertEqual(sum(sum(v.values()) for v in s["wins"].values()), 6)
        self.assertEqual(s["played"]["random"]["super_hunter"], 2)
        self.assertEqual(s["llm"]["turns"], s["llm"]["ok"])
        self.assertEqual(len(r["solo"]), 3 * 4)
        self.assertIn("random", s["solo_shots"])
        text = arena.report(r)
        self.assertIn("wins (row beat column)", text)
        self.assertIn("llm reasoner:", text)
        json.dumps(r)


if __name__ == "__main__":
    unittest.main()
