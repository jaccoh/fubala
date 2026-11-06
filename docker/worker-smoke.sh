#!/bin/sh
# Worker image smoke: the in-build check and the restricted-pod check run this
# same file, so they cannot drift apart.
set -eu

test "$(id -u)" = 10003
"$(uv python find 3.12)" -V
"$(uv python find 3.15)" -V
node -v
claude --version
git --version
python3 -c "import ctypes; ctypes.CDLL('libgomp.so.1')"
pgdir="$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -n 1)"
test -n "$pgdir"
echo "postgres bindir: $pgdir"
"$pgdir/initdb" --version
python3 -c "import ctypes; ctypes.CDLL('libpq.so.5')"
test -w "$UV_PYTHON_INSTALL_DIR"
code-review-graph --version
for compiler in cc gcc c++; do
    if command -v "$compiler" >/dev/null 2>&1; then
        echo "worker image must not contain a C compiler, found: $compiler" >&2
        exit 1
    fi
done
echo "worker smoke ok: uid $(id -u)"
