#!/usr/bin/env bash
# Deploy a specific image tag on this machine (runs on the VM, called by the pipeline).
#
#   IMAGE=ghcr.io/<owner>/rf-alert-manager IMAGE_TAG=<sha> deploy/deploy.sh
#
# Optional env: POSTGRES_PASSWORD (writes the secrets part of .env), DEPLOY_DIR.
set -euo pipefail

: "${IMAGE_TAG:?IMAGE_TAG is required}"
DEPLOY_DIR="${DEPLOY_DIR:-$HOME/rfam}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$DEPLOY_DIR/deploy"
# The compose project lives in a stable directory, not in the runner's workspace
# (which is wiped on every job). Volumes are tied to the project name "rfam".
if [ "$REPO_DIR" != "$DEPLOY_DIR" ]; then
  cp "$REPO_DIR/compose.yaml" "$DEPLOY_DIR/compose.yaml"
  cp "$REPO_DIR/deploy/"*.sh "$REPO_DIR/deploy/Caddyfile" "$DEPLOY_DIR/deploy/"
fi
cd "$DEPLOY_DIR"
touch .env releases.log
chmod 600 .env

if [ -n "${POSTGRES_PASSWORD:-}" ]; then
  grep -vE '^(POSTGRES_PASSWORD|POSTGRES_USER|POSTGRES_DB)=' .env > .env.tmp || true
  {
    echo "POSTGRES_USER=${POSTGRES_USER:-rfam}"
    echo "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}"
    echo "POSTGRES_DB=${POSTGRES_DB:-rfam}"
  } >> .env.tmp
  mv .env.tmp .env
fi

IMAGE="${IMAGE:-$(grep -E '^IMAGE=' .env | cut -d= -f2- || true)}"
: "${IMAGE:?IMAGE is required on first deploy}"
grep -vE '^(IMAGE|IMAGE_TAG)=' .env > .env.tmp || true
{ echo "IMAGE=${IMAGE}"; echo "IMAGE_TAG=${IMAGE_TAG}"; } >> .env.tmp
mv .env.tmp .env
chmod 600 .env

echo "==> deploying ${IMAGE}:${IMAGE_TAG}"
docker compose -p rfam pull ingest alerts
docker compose -p rfam up -d --no-build --remove-orphans --wait --wait-timeout 180

# Remember what was deployed, so rollback knows the previous version.
if [ "$(tail -n1 releases.log)" != "$IMAGE_TAG" ]; then
  echo "$IMAGE_TAG" >> releases.log
fi
docker image prune -f >/dev/null || true
echo "==> deployed ${IMAGE_TAG}"
