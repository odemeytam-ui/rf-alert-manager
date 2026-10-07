#!/usr/bin/env bash
# Creates the VM and registers it as a GitHub Actions self-hosted runner.
# Run from the repo root on your machine (macOS, Linux, or WSL on Windows):
#
#   infra/vm/provision.sh
#
# Needs: multipass, gh (logged in, admin on the repo).
# On WSL, Multipass is the Windows program: MULTIPASS=multipass.exe infra/vm/provision.sh
set -euo pipefail

VM_NAME="${VM_NAME:-rfam}"
MP="${MULTIPASS:-multipass}"
REPO="${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
CLOUD_INIT="infra/vm/cloud-init.yaml"
[[ "$MP" == *.exe ]] && CLOUD_INIT="$(wslpath -w "$CLOUD_INIT")"

if ! "$MP" info "$VM_NAME" >/dev/null 2>&1; then
  echo "==> creating VM $VM_NAME (Ubuntu 24.04)"
  "$MP" launch 24.04 --name "$VM_NAME" --cpus 2 --memory 4G --disk 20G \
    --cloud-init "$CLOUD_INIT" --timeout 900
fi

echo "==> waiting for cloud-init to finish"
# exit code 2 means "done with warnings", which is fine
"$MP" exec "$VM_NAME" -- cloud-init status --wait || [ $? -eq 2 ]

echo "==> registering the GitHub runner for $REPO"
TOKEN="$(gh api -X POST "repos/$REPO/actions/runners/registration-token" -q .token)"
"$MP" exec "$VM_NAME" -- sudo -iu ubuntu /opt/rfam/install-runner.sh "https://github.com/$REPO" "$TOKEN"

IP="$("$MP" info "$VM_NAME" | awk '/IPv4/ {print $2; exit}' | tr -d '\r')"
echo
echo "VM ready: $VM_NAME at $IP"
echo "After the first pipeline run the service is at http://$IP/health"
