# Story 903d86fe — verify evidence: backend suite under pytest-xdist

All numbers below are real runs on the story branch (`fubala/903d86fe53c6450597fed7c532662477`),
invoked from the repo root as `uv run --directory backend <pytest args>` (identical to
`cd backend && <same args>`); the timing runs were driven by a small `subprocess` wrapper that
stopped the clock around the whole `uv run` invocation. Each run's raw tail is quoted.

## Collected counts (identical in every mode)

| invocation | collected |
| --- | --- |
| `uv run pytest -q --collect-only -q` | 503 items / **579 collected** at run time (parametrized cases expand to 579) |
| `uv run pytest -q -n auto --maxprocesses=4 --collect-only -q` | 503 items / **579 collected** |
| `uv run pytest -q -p no:xdist --collect-only -q` | 503 items / **579 collected** |

(`--collect-only -q` reports per-file totals summing to 503 test items; the run-time
"collected / ran" figures are 579 because a few tests parametrize. All three modes report
exactly the same numbers, so xdist neither hides nor adds tests.)

## Wall times (labeled, as required by the story)

| label | invocation | result | wall time |
| --- | --- | --- | --- |
| local serial (baseline) | `uv run pytest -q` | 579 passed, 3 warnings | **362.81s (0:06:02)** |
| local xdist run 1/3 | `uv run pytest -q -n auto --maxprocesses=4` | 579 passed, 3 warnings | **108.87s (0:01:48)** |
| local xdist run 2/3 | `uv run pytest -q -n auto --maxprocesses=4` | 579 passed, 3 warnings | **133.55s (0:02:13)** |
| local xdist run 3/3 | `uv run pytest -q -n auto --maxprocesses=4` | 579 passed, 3 warnings | **137.08s (0:02:17)** |
| local no-xdist plugin fallback | `uv run pytest -q -p no:xdist` | 579 passed, 3 warnings | **416.02s (0:06:56)** |

Speedup, 3-run xdist mean 126.5s vs 362.81s serial: **~2.9x**. All five runs exit 0; raw tails:

```
579 passed, 3 warnings in 362.81s (0:06:02)          # serial baseline
579 passed, 3 warnings in 108.87s (0:01:48)          # xdist 1/3
579 passed, 3 warnings in 133.55s (0:02:13)          # xdist 2/3
579 passed, 3 warnings in 137.08s (0:02:17)          # xdist 3/3
579 passed, 3 warnings in 416.02s (0:06:56)          # -p no:xdist fallback
```

CI numbers (Docker test-stage step) are pending until the image build for this branch runs;
per the spec they must be read from the **Docker test-stage step**, not the serial `ci.yml`
job — `.gitea/workflows/ci.yml` stays untouched (story 8badd028 owns it) and stays serial
until the follow-up lands.

## Other checks

- `uv sync --locked` from the committed lock: OK ("Resolved 89 packages in 1ms / Checked 85
  packages in 1ms").
- `npx tsc -b --noEmit` (frontend/): exit 0. `npm run build`: exit 0 ("built in 341ms").
- `git diff origin/main...HEAD` touches no file under `frontend/`; `git status` clean after the
  build (no dist/ artifacts in the diff).
- Isolation audit: `grep -rn "chdir" backend/tests backend/app` — zero hits. Remaining
  `/tmp/` literals in tests are all fake, never-created paths or per-test values passed as
  strings (`test_delivery_push.py:214,220`, `test_model_gate.py:506`,
  `test_execution.py:1693-1694`, `test_selfupdate.py:77-147`) — none is a shared DB or socket.
  No test writes `git config --global`. Zero `xdist_group` markers, zero new skips: the only
  `skipif` markers (`test_run_tests_tool.py:458,494`, `test_selfupdate.py:304,347,432`) are
  byte-identical to `origin/main` (verified via `git diff origin/main...HEAD` on those files:
  empty).
- No deleted tests: `def test_` counts per rewritten file are equal to `origin/main`
  (`test_stories.py` 24=24, `test_execution.py` 47=47, `test_external_execution.py` 59=59,
  `test_delivery_push.py` 22=22, `test_epics.py` 5=5); all other files only gain tests
  (e.g. `test_auth.py` 28→34, `test_spec_preflight.py` 33→33,
  `test_run_verify_tool.py` 18→23, `test_story_kinds.py` 11→13).
- `make test` (Makefile:14) and `.gitea/workflows/ci.yml` are untouched by this diff:
  `git diff origin/main...HEAD -- Makefile .gitea/workflows/ci.yml` is empty.
- `make lint` / `uv run ruff check .` reports findings only in pre-existing `app/` files this
  branch does not modify (`app/agent.py`, `app/pr.py`, `app/routers/stories.py`,
  `app/runner.py`); the diff proves no `app/` file is touched. Cleaning those is another
  story, not this one.

## Post-merge settings swap (named gate, outside this diff — coordinator)

The worktree has no runtime DB (`backend/data/` is not in git), so the live strings must be
read at merge time via the API or sqlite. Exact swap for BOTH settings:

- app-level `verify_command` (default in `settings_store.py:53` is `""`; when a command is
  configured it is the suite string)
- Fubala project `test_command` (per-project override, stored in the projects table)

```
before: uv run pytest -q
after:  uv run pytest -q -n auto --maxprocesses=4
```

Everything else in each setting must stay byte-identical. If the live DB shows a different
`before` string (flags already present), stop and reconcile instead of overwriting. Until this
swap happens the pipeline verify/land-retest keep running serially; forgetting it silently
keeps the pipeline slow — it is a named merge gate, not polish.
