# Fubala: operational index

Fubala is the self-hosted AI story pipeline and kanban board. It is the coordination board for the
owner's projects, and the ems is its main user. Read `README.md` first (design principles,
lifecycle), then `FUBALA-QUICKSTART.md`.

## Roles (owner, 22 Oct 2026)

- **The owner is product owner and leader.** He sets the requirements and decides product
  questions.
- **Claude is the senior engineer.** Claude steers the board: it writes specs as requirements with
  acceptance criteria, coordinates, reviews every diff and verify output, and approves or rejects.
- **The coder is a cheap model.** Today that is GLM via the z.ai Coding Plan, provider `zai` at
  `https://api.z.ai/api/anthropic`. Claude does not code inside Fubala.

## Layout and commands

- `backend/`: FastAPI with the pipeline in-process (`app/runner.py`). Start it with
  `cd backend && uv run python -m app`; it listens on port 8000 and serves `frontend/dist` when
  built.
- `frontend/`: Vite and React. `make frontend` runs the dev server on :5173.
- MCP is served by the backend itself over streamable HTTP at `/mcp`, behind the same Bearer auth
  as `/api/v1`: `claude mcp add --transport http fubala https://fubala.example.com/mcp --header
  "Authorization: Bearer <token>"`. Every `/api/v1` route has a tool (`app/mcp_tools.py`; a test
  enforces it, only health, ping, `POST /auth/session` and the token rotate/clear routes are excluded: a token
  value never enters an LLM context). The stdio process
  `python -m app.mcp` is now a thin proxy to `/mcp` (`FUBALA_MCP_URL`, `FUBALA_API_TOKEN`) and is
  deprecated for one release.
- Tests: `make test` (backend pytest) and `make test-frontend` (vitest). Lint: `make lint` (ruff).
- State lives in SQLite at `FUBALA_DATABASE_PATH`. Runtime settings and provider keys are stored
  in the DB, not in env.

## Deploy (k3s)

- **Image.** `Dockerfile` (tests run in the build), built by `.gitea/workflows/build.yaml` with
  the kaniko house pattern, tagged `<registry>/fubala/fubala:<date>-<run>`. It runs on
  pushes to `main` (paths-filtered) and on `workflow_dispatch`: the land job in `ci.yml`
  dispatches it after its own merges, so every landed story gets an image. The image build skips
  the suite (`--build-arg SKIP_TESTS=1`) when CI verified the same tree (dispatch: `tested_sha`
  has the same tree and a green `ci / test (pull_request)`; push: the push-event CI status turns
  green within 25 min); any doubt runs the tests. `docker build --target test .` still runs them.
- **CI and auto-land.** `.gitea/workflows/ci.yml` runs on the house runners
  (`[self-hosted, amd64]`) on every push to `main` and every PR targeting `main`. It has one job,
  `test` (the story-verify suite: backend `uv sync --locked && uv run pytest -q`, frontend
  `npm ci && npx tsc -b --noEmit && npm run build`), run in a pod like ems's `test.yaml`: a
  `git-clone` initContainer plus the two suites as real `containers:` entries (a Pod with
  initContainers only is invalid Kubernetes and can never start). **The
  required status check in branch protection on `main` is `ci / test`** (one job, one name).
  The `land` job then merges green PRs itself (merge `main` into the head branch, re-run the
  same tests, merge via the Gitea API with `secrets.GITEA_TOKEN`). **PRs touching `.gitea/` are
  never auto-merged**: the land job exits "waiting for coordinator" and a human merges them.
  Merges by the actions token start no workflows, so a land-job merge is followed by a
  `workflow_dispatch` of `build.yaml` via the API with the same token (`{"ref":"main"}`); a
  coordinator merge triggers it via the normal push event. The image tag is
  `YYYYMMDD-<run_id>`.
- **CI pods must mirror the Dockerfile's uv provisioning** (`UV_PYTHON_PREFERENCE=only-managed`,
  `UV_PYTHON_INSTALL_DIR`, `uv python install 3.12`): the suite downloads 3.13 on demand for
  `tests/test_run_tests_tool.py`, and `only-system` makes uv refuse (run 2979).
- **Known flaky tests** (found 24 Oct 01:00–05:30 on the shared house node; found by the
  coordinator). Timing-sensitive tests that pass locally and in isolation but fail a loaded CI
  run. Until each is fixed for real, the CI gates rerun a red test up to 2x
  (`--reruns 2 --reruns-delay 2` from `pytest-rerunfailures`, in ci.yml test job, ci.yml land
  job and the Dockerfile test stage; NOT in the runtime story `verify_command` — the coder must
  still see the flake). The plugin prints `RERUN` lines; the CI jobs grep them into the job log
  and the step summary so the flakes stay known. The four:
  - `test_execution::test_missing_verdict_retry_recovers_approval` — sleep-plus-deadline poll
    races the async pipeline; wait on an event the pipeline sets instead.
  - `test_execution::test_cancel_aborts_inflight_coder_run` — cancel must not race the coder
    stage mid-round (the stories.py:2133 comment cites it by this name); event-based wait on the
    pipeline's parked state, not a sleep window.
  - `test_stories::test_reject_feeds_back_into_next_spec_run` — same scheduler race; hard-off
    scheduler or event-based wait instead of sleep-plus-deadline.
  - `test_external_execution::test_toggling_project_to_external_affects_queued_stories`
    (formerly `test_toggling_project_to_external`, cited fixed in #73 — it raced again on
    24 Oct) — a scheduler hard-off (the scheduler picking up the story mid-toggle), not a sleep.
  Fix direction for all four: a scheduler hard-off or an event-based wait instead of
  sleep-plus-deadline. When a test is fixed, remove it from this list AND drop the rerun flag
  from both ci.yml pytest commands and the Dockerfile test stage.
- **Deployment env** (set per deployment, read at call time): `FUBALA_WORKER_IMAGE_REPO` (worker
  image repo, no tag; empty = no pods, runs stay in-process), `FUBALA_STORY_LABEL` (default
  `fubala.dev/story`), `FUBALA_DEV_NODE_LABEL` (empty = no toleration or affinity), and for the
  entrypoint `GIT_PUBLIC_HOST`, `GIT_INTERNAL_URL`, `GIT_AUTHOR_EMAIL`, `FUBALA_DATA_DIR`.
- **Manifests.** In the deployment repo (`homelab2`): `infrastructure/apps/fubala/` plus the ArgoCD app. The
  ingress host is internal only.
- **The pod's entrypoint** (`docker/entrypoint.sh`):
  - clones `FUBALA_REPOS` into `/data/repos`;
  - points git at the in-cluster Gitea;
  - reads the bot token from a mounted secret.
- **Self-update** (`selfupdate.py`) is for the laptop install only. Keep `self_update_enabled` off
  in the pod; an image bump through a homelab2 PR replaces it.
- **The pod runs AI-written code.** Keep its blast radius small:
  - The backend has a namespaced ServiceAccount token whose Role covers only `pods`, `pods/exec`
    and `pods/log`, so it can run per-run worker pods (`run_executor=verify|pod`, `app/pods.py`).
    Backend egress to the kube-apiserver is allowed by a CiliumNetworkPolicy.
  - Worker pods get no token and no credentials (`automountServiceAccountToken: false`, no
    `envFrom`, no secret volume). Their egress is DNS, Gitea and the internet on 443 only.
  - Without the token (or `POD_NAME`/`POD_UID`/`NODE_NAME`, or `worker_image`) the backend runs
    everything in-process. Keep it that way.

## Rules

- Never commit on main: use a feature branch and a PR, and merge through the Gitea API
  (`~/.gitea-token`).
- No emojis. Code, logs and UI are in English. The owner writes short Dutch.
- Every finding becomes a story on the board at once.
- All tests are green in the image build; no macOS-only deselections remain.
