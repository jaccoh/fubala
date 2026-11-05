#!/bin/sh
# Prepares the pod's persistent home and project clones, then starts Fubala.
#
#   FUBALA_REPOS        space-separated name=owner/repo; each is cloned to
#                       /data/repos/<name> when missing (never touched after)
#   GITEA_TOKEN_FILE    file holding the Gitea token; git reads it per use
#                       through a credential helper, it is never written out
#   GIT_INTERNAL_URL    in-cluster Gitea base URL; git.hoeve.nu remotes are
#                       rewritten to it so the pod needs no LAN access
set -eu

mkdir -p "$HOME" /data/repos /data/cache

git config --global user.name "${GIT_AUTHOR_NAME:-fubala}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-fubala@hoeve.nu}"
git config --global init.defaultBranch main

if [ -n "${GITEA_TOKEN_FILE:-}" ]; then
  git config --global credential.helper \
    "!f() { echo username=${GITEA_USER:-fubala-bot}; printf 'password=%s\n' \"\$(cat '$GITEA_TOKEN_FILE')\"; }; f"
fi

if [ -n "${GIT_INTERNAL_URL:-}" ]; then
  git config --global --unset-all "url.$GIT_INTERNAL_URL/.insteadOf" || true
  git config --global --add "url.$GIT_INTERNAL_URL/.insteadOf" "git@git.hoeve.nu:"
  git config --global --add "url.$GIT_INTERNAL_URL/.insteadOf" "ssh://git@git.hoeve.nu/"
  git config --global --add "url.$GIT_INTERNAL_URL/.insteadOf" "https://git.hoeve.nu/"
fi

for spec in ${FUBALA_REPOS:-}; do
  name=${spec%%=*}
  repo=${spec#*=}
  if [ ! -d "/data/repos/$name/.git" ]; then
    echo "fubala-entrypoint: cloning $repo into /data/repos/$name"
    git clone --quiet "https://git.hoeve.nu/$repo.git" "/data/repos/$name"
  fi
done

exec "$@"
