#!/usr/bin/env bash
# Roll back to a previous image. Run on the VM, or through the "Rollback" workflow.
#
#   deploy/rollback.sh            # back to the version before the current one
#   deploy/rollback.sh <sha>      # to a specific version
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-$HOME/rfam}"
cd "$DEPLOY_DIR"

if [ -n "${1:-}" ]; then
  TARGET="$1"
else
  CURRENT="$(grep -E '^IMAGE_TAG=' .env | cut -d= -f2-)"
  TARGET="$(grep -vx "$CURRENT" releases.log | tail -n1 || true)"
fi
[ -n "${TARGET:-}" ] || { echo "no previous release found in releases.log" >&2; exit 1; }

echo "==> rolling back to ${TARGET}"
IMAGE_TAG="$TARGET" DEPLOY_DIR="$DEPLOY_DIR" bash "$DEPLOY_DIR/deploy/deploy.sh"
