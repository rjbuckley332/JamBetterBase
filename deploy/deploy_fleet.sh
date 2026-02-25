#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./deploy_fleet.sh <git-tag-or-branch> [inventory_json] [batch_size] [dry_run:0|1]

TAG="${1:-}"
INV="${2:-/home/nds/deploy/fleet_inventory.json}"
BATCH="${3:-5}"
DRY="${4:-0}"

if [[ -z "$TAG" ]]; then
  echo "Usage: $0 <git-tag-or-branch> [inventory_json] [batch_size] [dry_run:0|1]"
  exit 1
fi

if [[ ! -f "$INV" ]]; then
  echo "Inventory not found: $INV"
  exit 1
fi

echo "[deploy_fleet] tag=$TAG inventory=$INV batch=$BATCH dry=$DRY"
exec /home/nds/deploy/canary_rollout.py "$TAG" "$INV" "$BATCH" "$DRY"
