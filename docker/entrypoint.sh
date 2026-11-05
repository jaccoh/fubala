#!/bin/sh
# Prepares the pod's persistent home and project clones, then starts Fubala.
#
#   FUBALA_REPOS        space-separated name=owner/repo; each is cloned to
#                       $FUBALA_DATA_DIR/repos/<name> when missing (never touched
#                       after); needs GIT_PUBLIC_HOST
#   GIT_PUBLIC_HOST     public host of the git server (e.g. git.example.com)
#   GIT_INTERNAL_URL    in-cluster git base URL; remotes on GIT_PUBLIC_HOST are
#                       rewritten to it so the pod needs no LAN access (the
#                       rewrite needs both GIT_INTERNAL_URL and GIT_PUBLIC_HOST)
#   GITEA_TOKEN_FILE    file holding the Gitea token; git reads it per use
#                       through a credential helper, it is never written out
#   GIT_AUTHOR_NAME / GIT_AUTHOR_EMAIL   commit identity (fubala / fubala@localhost)
#   FUBALA_DATA_DIR     persistent data root (default /data)
set -eu

data=${FUBALA_DATA_DIR:-/data}

if [ -n "${FUBALA_REPOS:-}" ] && [ -z "${GIT_PUBLIC_HOST:-}" ]; then
  echo "fubala-entrypoint: FUBALA_REPOS is set but GIT_PUBLIC_HOST is not" >&2
  exit 1
fi

mkdir -p "$HOME" "$data/repos" "$data/cache"

git config --global user.name "${GIT_AUTHOR_NAME:-fubala}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-fubala@localhost}"
git config --global init.defaultBranch main

if [ -n "${GITEA_TOKEN_FILE:-}" ]; then
  git config --global credential.helper \
    "!f() { echo username=${GITEA_USER:-fubala-bot}; printf 'password=%s\n' \"\$(cat '$GITEA_TOKEN_FILE')\"; }; f"
fi

if [ -n "${GIT_INTERNAL_URL:-}" ] && [ -n "${GIT_PUBLIC_HOST:-}" ]; then
  git config --global --unset-all "url.$GIT_INTERNAL_URL/.insteadOf" || true
  git config --global --add "url.$GIT_INTERNAL_URL/.insteadOf" "git@$GIT_PUBLIC_HOST:"
  git config --global --add "url.$GIT_INTERNAL_URL/.insteadOf" "ssh://git@$GIT_PUBLIC_HOST/"
  git config --global --add "url.$GIT_INTERNAL_URL/.insteadOf" "https://$GIT_PUBLIC_HOST/"
fi

for spec in ${FUBALA_REPOS:-}; do
  name=${spec%%=*}
  repo=${spec#*=}
  if [ ! -d "$data/repos/$name/.git" ]; then
    echo "fubala-entrypoint: cloning $repo into $data/repos/$name"
    git clone --quiet "https://$GIT_PUBLIC_HOST/$repo.git" "$data/repos/$name"
  fi
done

exec "$@"
