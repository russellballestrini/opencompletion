# 0001: Jev Battleship arena, resume when TypeSafe credits return

**Status:** blocked (vendor billing). **Opened:** 2026-09-19. **Owner:** fox.

## Where it stands

TypeSafe (jev) answers HTTP 402 "no available API credits" since about
01:00 UTC 2026-09-19, no ETA. Everything that does not need jev is done &
pushed; everything below resumes with one command each once credits exist.

Shipped on `main` (8 commits from f213c41 to faaed8e, CI green):

- `classifier.py`: jev client, same env vocabulary as uncloseai-cli &
  unhomeschool, fail-open into `activity.categorize_response` &
  `research/guarded_ai.py`. Knob `OPENCOMPLETION_CLASSIFIER`.
- `jev_hunter.py`: exact placement-density grid + one jev decision per
  turn, fused (`JEV_WEIGHT` 0.4, `CANDIDATES` 6). `make jev-bench`.
- `battleship_modes.py`: the five admirals, shared by the activity
  (`research/activity29-battleship.yaml`, `activity29-testship.yaml`) & the
  arena. Hunter & Super Human Hunter rebuilt at their tier's best.
- `battleship_arena.py` + `make arena`: seeded round robin, solo clears,
  per-turn model health. Results of the 2026-09-19 run live in the blog
  repo: `content/uploads/2024/battleship-solvers/arena_results.json`.
- `research/jev_playstyles.py`: nine jev-with-grid variants, paired solo +
  round robin. Its only run so far hit 402 on every call: **discard**.

Blog draft (status `draft`, builds clean):
`~/git/russell.ballestrini.net/content/2026-09-18-battleship-arena-five-admirals-one-grid.rst`
with charts from `plots_for_arena_blog_post.py` beside the 2024 data.

## What the 2026-09-19 arena run is & is not

- LLM leg clean: Qwen3.8-27B (MODEL_2, hermes 502 all evening) answered
  3,421/3,421 turns at 2 workers, `LLM_TIMEOUT_S=45`.
- Jev head-to-head leg clean: jev decided 1,658/1,658 versus turns.
- Jev solo leg missing: all 40 solo boards fell to the grid (402), so the
  jev solo row equals Super Human Hunter's. The post says so.
- `config.llm_model` in the JSON shows the default id, not the model that
  actually answered (the fallback chain resolved Qwen). Small defect:
  record the resolved model per endpoint in `battleship_arena.run`.

## Resume steps

1. Confirm credits: `make classifier-check` after `source vars.sh`.
2. Jev solo leg only (about 1,800 calls):
   `source vars.sh && venv/bin/python battleship_arena.py --games 10 --seed 0 --workers 2 --modes jev_reasoner`
   then merge its `solo` rows for jev into the blog's `arena_results.json`
   (same seed, same boards) & re-render the charts.
3. Playstyle trial (about 8,800 calls, fox flagged the 10k line):
   `source vars.sh && venv/bin/python research/jev_playstyles.py --boards 20 --workers 4 --out jev_playstyles.json`
   Report `jev turns` > 0 & `errors` 0 before believing any row.
4. Fill the post's "Where this goes" with the trial's answer, flip
   `:status: draft` to `published`, `make clean && make html && make formats`,
   commit & push the site (its CI deploys).
5. Wider run when budget allows: 30 games per pair (about 12,000 jev calls,
   over the 10k line, ask first).

## Open design questions (fox's call)

- `JEV_WEIGHT` 0.4 was a judgment, not measured; the trial measures it.
- Sampled-posterior grid from the 2024 million-board corpus (no vendor,
  ~2 shots/fleet expected gain).
- Opponent placement-style classification with jev (the jev-native
  playstyle; needs non-uniform boards or real game logs to test).
- Rate the chat model at 2 workers only; 8 workers starved it (28% answered).

## Security note

Credit exhaustion looked like a key leak. Audit 2026-09-19 01:30 UTC on
this box found none: git history placeholders only, no shell history hits,
transcripts hold only placeholder values, key file mode 600. `vars.sh` is
mode 644 (recommend 600). Our own usage that day was roughly 4,000 to
6,000 calls; if the console shows far more, rotate the key.
