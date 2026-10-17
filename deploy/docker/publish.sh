#!/usr/bin/env bash
# Build the multi-arch image locally and push it to GHCR (what CI does on main).
# Needs a token with write:packages:  gh auth refresh -s write:packages
#   deploy/docker/publish.sh            # tags :<short sha>
#   deploy/docker/publish.sh latest     # also tags :latest
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
image=ghcr.io/lamont/microcast
sha=$(git rev-parse --short HEAD)
if [[ -n $(git status --porcelain) ]]; then
  echo "working tree is dirty; commit first so the tag matches the source" >&2
  exit 1
fi

tags=(-t "$image:$sha")
for extra in "$@"; do tags+=(-t "$image:$extra"); done

gh auth token | docker login ghcr.io -u "$(gh api user -q .login)" --password-stdin

# Docker Desktop's default builder can't export a multi-platform image; use a container builder.
docker buildx inspect microcast >/dev/null 2>&1 || docker buildx create --name microcast --driver docker-container
docker buildx build --builder microcast \
  --platform linux/amd64,linux/arm64 \
  -f deploy/docker/Dockerfile \
  --build-arg GIT_SHA="$sha" \
  --label org.opencontainers.image.source=https://github.com/lamont/microcast \
  --label org.opencontainers.image.revision="$(git rev-parse HEAD)" \
  "${tags[@]}" --push .
