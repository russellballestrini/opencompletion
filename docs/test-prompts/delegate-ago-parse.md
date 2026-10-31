# Test prompt: delegate adds parse() to ago

A large end-to-end check of tools in one chat (2026-10-31): web search,
`delegate` (a long uncloseai-cli run: summaries, vitals, the job fold),
a final `sandbox_shell` check, and `sandbox_share_file` handing back an
image inline and a zip. Run it as an egress user, in a **new** chat (a new
sandbox from the newest golden image). Needs the 60-minute delegate limit
(`OPENCOMPLETION_DELEGATE_TIMEOUT_S`, default 3600 since 014d7ab); at 900s
it timed out at 3 of 9 plan steps.

## Prompt

```text
Find the `ago` Python library (human-readable timedeltas, by Russell Ballestrini) and its git repo. Use delegate to add the inverse of `human()`: a new `parse(text, now=None)` that turns strings like "3 hours ago", "in 2 days, 4 hours", "1 year, 2 months ago" and the abbreviated forms `human()` produces back into a `datetime`. Done means:
1. `parse()` round-trips: for at least 30 generated timedeltas (seconds through years, past and future, `precision` 1–4, abbreviated or not), `parse(human(d, ...))` lands within the precision `human()` dropped. Write that as a pytest test.
2. At least 10 more focused tests: odd spacing, capitals, "a minute ago", "just now", and bad input raising `ValueError`.
3. The full suite passes, old tests included.
4. A benchmark script times `human()` and `parse()` over 10,000 values and saves a bar chart to `bench.png`.
5. A short `PARSE.md` explains the grammar `parse()` accepts.

When delegate is done, use the shell once to rerun the tests yourself and confirm the pass count. Then share `bench.png` (show it inline) and a zip of the repo's changes (`git diff` plus the new files) with me.
```

## What to watch

- After the search the model hands the whole job to delegate (setup
  included) rather than cloning first.
- The delegate card: a summary about every 15s, the job fold (plan, files
  changed, model health, tokens, time) & vitals chips filling in.
- A final shell check that reruns the tests itself.
- Two shared files: `bench.png` shown inline, the zip as a download link.

## Traps the run found (2026-10-31, first try)

- `human()` on a `datetime` measures against the real clock, so a
  round-trip test with a fixed `now` drifts; the round-trip has to use
  `timedelta`s.
- `human()` has no month unit (a year is 365 days, months fold into
  days); a parser using 365/12-day months fails every multi-unit trip.
- The golden image's pytest loads a xonsh plugin; `-p no:xonsh` runs clean.
