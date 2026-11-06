# Fubala worker image (docs/worker-pods-design.md): the toolchain a story
# round needs, no Fubala code, no credentials. Multi-arch (amd64 + arm64) via
# .gitea/workflows/worker-image.yaml. NODE_IMAGE, UV_IMAGE and the claude pin
# must equal the runtime Dockerfile's, and the code-review-graph pin must equal
# backend/pyproject.toml's [tool.fubala] (backend/tests/test_worker_image_parity.py).
ARG NODE_IMAGE=node:24.22.1-bookworm-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.6

FROM ${UV_IMAGE} AS uv

FROM ${NODE_IMAGE} AS worker
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python UV_PYTHON_PREFERENCE=only-managed \
    UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tini libgomp1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
RUN groupadd --gid 10003 fubala \
 && useradd --system --uid 10003 --gid 10003 --home-dir /home/fubala --create-home fubala \
 && mkdir -p /opt/uv-python /opt/claude-code /opt/uv-tools \
 && chown -R 10003:10003 /opt/uv-python /opt/claude-code /opt/uv-tools /home/fubala
USER 10003
ENV HOME=/home/fubala
RUN uv python install 3.12 3.15 \
 && UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/opt/uv-tools/bin uv tool install code-review-graph==2.3.9 \
 && npm_config_cache=/tmp/npm-build-cache npm install -g --prefix /opt/claude-code @anthropic-ai/claude-code@2.1.314 \
 && rm -rf /tmp/npm-build-cache
USER root
RUN ln -s "$(su -s /bin/sh fubala -c 'uv python find 3.12')" /usr/local/bin/python3 \
 && ln -s /usr/local/bin/python3 /usr/local/bin/python
# PostgreSQL server binaries + libpq for project verify suites (ems S4
# ec696e73: tools/ephemeral_pg.sh runs initdb from /usr/lib/postgresql/*/bin).
# One layer on purpose; the runtime Dockerfile carries the same block. After
# the python3 symlinks because the ctypes proof needs /usr/local/bin/python3.
# No service, no cluster: the drop-in stops the postinst from creating `main`.
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
ENV PATH=/opt/claude-code/bin:/opt/uv-tools/bin:$PATH DISABLE_AUTOUPDATER=1
COPY --chown=10003:10003 docker/worker-smoke.sh /usr/local/bin/worker-smoke
USER 10003
# A red smoke fails the build: no image.
RUN /usr/local/bin/worker-smoke
ENTRYPOINT ["tini","--"]
USER 10003
