# Fubala server image for the homelab k3s (homelab2: infrastructure/apps/fubala).
# One process: API + built UI + the story pipeline. The pipeline runs git,
# uv (verify downloads its own Pythons) and npm inside story worktrees, so the
# runtime carries git, uv and node.
ARG NODE_IMAGE=node:24.22.1-bookworm-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.6

FROM ${UV_IMAGE} AS uv

FROM ${NODE_IMAGE} AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ${NODE_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python UV_PYTHON_PREFERENCE=only-managed \
    UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
# proof the pinned uv can download the CPython that project verifies pin (3.15.2);
# grep alone decides the exit code (dash, no pipefail)
RUN uv --version \
 && uv python list --all-versions > /tmp/pylist \
 && grep -q cpython-3.15.2 /tmp/pylist \
 && rm /tmp/pylist
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
# the coder's allowlisted `python`/`python3` commands need one on PATH
RUN uv python install 3.12 && uv sync --locked --no-dev --no-install-project \
 && ln -s "$(uv python find 3.12)" /usr/local/bin/python3 \
 && ln -s /usr/local/bin/python3 /usr/local/bin/python
COPY backend/app ./app
COPY backend/prompts ./prompts
COPY docs/mcp-instructions.md /app/docs/mcp-instructions.md

# Tests run inside the build: a red test means no image (house pattern, ems).
# --reruns: CI-only stopgap for the four known timing flakes (CLAUDE.md
# "Known flaky tests"); -rR prints named "RERUN test..." summary lines without
# shell gymnastics (the base /bin/sh is dash, no pipefail — never pipe pytest
# through grep: dash would lose the exit code and a red suite would ship).
FROM base AS test
# SKIP_TESTS=1 is set by build.yaml only when CI already verified this exact
# tree; the default (empty) always runs the suite.
ARG SKIP_TESTS=
COPY backend/tests ./tests
# The CI guard tests (test_ci_timeout_dump.py) read the workflow at
# <repo>/.gitea/workflows/ci.yml, i.e. /app/.gitea here. Test stage only: the
# runtime stage takes nothing but /tests-passed from this stage.
COPY .gitea /app/.gitea
# Worker-image parity test (test_worker_image_parity.py) reads both Dockerfiles.
COPY Dockerfile /app/Dockerfile
COPY docker/worker.Dockerfile /app/docker/worker.Dockerfile
# Every test passes on Linux now: the /proc stranger-guard tests (6ae30a41)
# and the take-in-main no-op gate (533c90b6) are green in the image build.
RUN if [ "${SKIP_TESTS}" = "1" ]; then \
      echo "tests skipped: CI-verified tree" && touch /tests-passed; \
    else \
      uv sync --locked \
      && git config --global user.name test && git config --global user.email test@local \
      && tini -s -- uv run --locked pytest -q -p no:cacheprovider -n auto --maxprocesses=4 \
           --reruns 2 --reruns-delay 2 -rR \
      && touch /tests-passed; \
    fi

FROM base AS runtime
# LightGBM (project verify suites, e.g. the ems learner tests) dlopens
# libgomp.so.1 at import time; bookworm-slim ships none, so those tests
# silently skip inside story verify. Install and load-proof it in ONE layer:
# the ctypes proof fails the build if the install was a silent no-op (same
# house pattern as the `claude --version` proof below).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/local/bin/python3 -c "import ctypes; ctypes.CDLL('libgomp.so.1')"
# PostgreSQL server binaries + libpq for project verify suites (ems S4
# ec696e73: tools/ephemeral_pg.sh runs initdb from /usr/lib/postgresql/*/bin).
# One layer on purpose; docker/worker.Dockerfile carries the same block. No
# service, no cluster: the drop-in stops the postinst from creating `main`.
# Three proofs: initdb runs (exit code from initdb itself), libpq loads, and
# no cluster directory exists.
RUN mkdir -p /etc/postgresql-common/createcluster.d \
 && echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.d/no-main-cluster.conf \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql postgresql-client libpq5 \
 && rm -rf /var/lib/apt/lists/* \
 && dir="$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -n 1)" \
 && test -n "$dir" && echo "postgres bindir: $dir" \
 && "$dir/initdb" --version \
 && /usr/local/bin/python3 -c "import ctypes; ctypes.CDLL('libpq.so.5')" \
 && for d in /var/lib/postgresql/*/main; do ! test -e "$d" || { echo "unexpected cluster $d"; exit 1; }; done
RUN useradd --system --uid 10003 --home-dir /data/home --no-create-home fubala
# Headless Claude Code CLI, installed AS THE NON-ROOT RUNTIME USER (fubala)
# into a writable prefix, so the story CODE stage can invoke `claude`
# (app/headless.py) in the worktree. Installed as non-root on purpose: no
# root-owned files, and the CLI cannot write outside its own prefix.
# The prefix lives OUTSIDE /data (24 Oct): the pod mounts its volume on /data,
# which hid the old /data/home/.npm-global install -- `claude` did not exist
# in the running pod. The build fails here if the binary does not run.
# PIN: @anthropic-ai/claude-code@2.1.314 — bump deliberately, never by tag;
# DISABLE_AUTOUPDATER keeps the pin. Fubala passes `--effort` per phase
# (app/headless.py, app/providers/claude_code.py); `--max-turns` is a hidden
# flag in this version (accepted, absent from `claude --help`).
RUN mkdir -p /opt/claude-code /data/home /data/cache \
 && chown -R 10003:10003 /opt/claude-code /data/home /data/cache \
 && su -s /bin/sh fubala -c \
      'npm_config_cache=/tmp/npm-build-cache npm install -g --prefix /opt/claude-code @anthropic-ai/claude-code@2.1.314 \
       && rm -rf /tmp/npm-build-cache && /opt/claude-code/bin/claude --version'
# code-review-graph (story code graph, app/code_graph.py) in its OWN env: it needs
# mcp<2 while the backend runs mcp 2. PIN: backend/pyproject.toml [tool.fubala].
RUN mkdir -p /opt/uv-tools && chown 10003:10003 /opt/uv-tools \
 && su -s /bin/sh fubala -c \
      'UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/opt/uv-tools/bin uv tool install code-review-graph==2.3.9 \
       && /opt/uv-tools/bin/code-review-graph --version'
ENV PATH=/opt/claude-code/bin:/opt/uv-tools/bin:$PATH DISABLE_AUTOUPDATER=1
COPY --from=test /tests-passed /tests-passed
COPY --from=frontend /src/frontend/dist /app/frontend/dist
COPY docker/entrypoint.sh /usr/local/bin/fubala-entrypoint
USER 10003
ENV HOME=/data/home FUBALA_HOST=0.0.0.0 FUBALA_PORT=8000 \
    FUBALA_DATABASE_PATH=/data/fubala.db UV_CACHE_DIR=/data/cache/uv \
    npm_config_cache=/data/cache/npm
EXPOSE 8000
# tini reaps orphans of detached children (git, verify subprocesses)
ENTRYPOINT ["tini", "--", "/usr/local/bin/fubala-entrypoint"]
CMD ["/app/backend/.venv/bin/python", "-m", "app"]
