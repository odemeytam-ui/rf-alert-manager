#!/usr/bin/env bash
# Creates the VM and registers it as a GitHub Actions self-hosted runner.
# Run from the repo root on your machine (macOS, Linux, or WSL on Windows):
#
#   infra/vm/provision.sh
#
# Needs: multipass, gh (logged in, admin on the repo).
# If Multipass is the Windows program (used from WSL): MULTIPASS=multipass.exe infra/vm/provision.sh
set -euo pipefail

VM_NAME="${VM_NAME:-rfam}"
MP="${MULTIPASS:-multipass}"
REPO="${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"

# Absolute path: Multipass may not resolve a relative path from our working directory,
# and the snap version of Multipass can only read files under $HOME.
CLOUD_INIT="$(cd "$(dirname "$0")" && pwd)/cloud-init.yaml"
[[ "$MP" == *.exe ]] && CLOUD_INIT="$(wslpath -w "$CLOUD_INIT")"

if ! "$MP" info "$VM_NAME" >/dev/null 2>&1; then
  echo "==> creating VM $VM_NAME (Ubuntu 24.04)"
  "$MP" launch 24.04 --name "$VM_NAME" --cpus 2 --memory 4G --disk 20G \
    --cloud-init "$CLOUD_INIT" --timeout 900
fi

# Make sure the configuration really reached the VM (an empty "#cloud-config {}"
# means Multipass could not read the file).
if ! "$MP" exec "$VM_NAME" -- sudo cloud-init query userdata | grep -q "^packages:"; then
  echo "ERROR: the VM did not receive infra/vm/cloud-init.yaml." >&2
  echo "Delete it ('$MP delete $VM_NAME --purge') and check that the file is readable by Multipass." >&2
  exit 1
fi

echo "==> waiting for cloud-init to finish"
"$MP" exec "$VM_NAME" -- cloud-init status --wait || true

if ! "$MP" exec "$VM_NAME" -- sh -c 'command -v docker' >/dev/null; then
  echo "ERROR: Docker is not installed in the VM; package installation failed." >&2
  echo "Most likely the VM has no internet. Check with: $MP exec $VM_NAME -- ping -c 2 8.8.8.8" >&2
  echo "On WSL2 the usual cause is the FORWARD firewall policy; fix it, then re-run setup:" >&2
  echo "  sudo sysctl -w net.ipv4.ip_forward=1 && sudo iptables -P FORWARD ACCEPT" >&2
  echo "  $MP exec $VM_NAME -- sudo cloud-init clean --logs --reboot   # then run this script again" >&2
  exit 1
fi

echo "==> registering the GitHub runner for $REPO"
TOKEN="$(gh api -X POST "repos/$REPO/actions/runners/registration-token" -q .token)"
"$MP" exec "$VM_NAME" -- sudo -iu ubuntu /opt/rfam/install-runner.sh "https://github.com/$REPO" "$TOKEN"

IP="$("$MP" info "$VM_NAME" | awk '/IPv4/ {print $2; exit}' | tr -d '\r')"
echo
echo "VM ready: $VM_NAME at $IP"
echo "After the first pipeline run the service is at http://$IP/health"
