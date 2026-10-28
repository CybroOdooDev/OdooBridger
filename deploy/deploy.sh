#!/usr/bin/env bash
# Deploy (or roll back to) one release of OdooBridger on this server.
#
#   deploy/deploy.sh ghcr.io/<owner> <tag>
#
# Run by the CI/CD workflow over SSH; can be run by hand the same way (log in to
# ghcr.io first if the images are private). It pulls the core and BioBridge
# images for <tag>, restarts them with the production compose, waits until both
# report healthy, and records the release. If they do not become healthy, it
# puts the previous release back and exits non-zero, so the workflow shows red.
#
# deploy/.env (secrets, domains) stays on the server and is never overwritten.
set -euo pipefail

REGISTRY="${1:?usage: deploy.sh ghcr.io/<owner> <tag>}"
TAG="${2:?usage: deploy.sh ghcr.io/<owner> <tag>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

[ -f .env ] || { echo "deploy/.env is missing on this server (copy .env.example and fill it in)"; exit 1; }

COMPOSE=(docker compose --env-file .env -f docker-compose.yml -f docker-compose.prod.yml)
RELEASE_FILE="$HERE/.release"
PREVIOUS="$(cat "$RELEASE_FILE" 2>/dev/null || true)"

up() {
  export CORE_IMAGE="$REGISTRY/odoobridger-core:$1"
  export BIOBRIDGE_IMAGE="$REGISTRY/odoobridger-biobridge:$1"
  "${COMPOSE[@]}" pull core biobridge
  # BioBridge brings its schema up to date before it starts serving.
  "${COMPOSE[@]}" up -d --no-build --remove-orphans
}

healthy() {
  local deadline=$((SECONDS + 180)) state
  for svc in core biobridge; do
    while :; do
      state="$(docker inspect -f '{{.State.Health.Status}}' "$("${COMPOSE[@]}" ps -q "$svc")" 2>/dev/null || echo missing)"
      [ "$state" = healthy ] && break
      [ $SECONDS -ge $deadline ] && { echo "$svc is $state after 3 minutes"; return 1; }
      sleep 5
    done
  done
}

echo "Deploying $TAG (previous: ${PREVIOUS:-none})"
up "$TAG"
if healthy; then
  echo "$TAG" > "$RELEASE_FILE"
  echo "$(date -u +%FT%TZ) $TAG" >> "$HERE/.release-history"
  docker image prune -f >/dev/null || true
  echo "Release $TAG is live."
else
  "${COMPOSE[@]}" logs --tail 80 core biobridge || true
  if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" != "$TAG" ]; then
    echo "Rolling back to $PREVIOUS"
    up "$PREVIOUS"
    healthy || echo "The previous release did not come back healthy either; check the server."
  fi
  exit 1
fi
