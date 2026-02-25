# JamBetter Deploy & Rollback Runbook (Developer Version)

## Scope
This runbook defines how to:
1. Deploy code changes via Git to JamBetter servers.
2. Roll back code changes quickly **without** reprovisioning servers.
3. Fall back to full snapshot restore only when host/runtime state is broken.

---

## Source of Truth (Git)

- **Repository:** `https://github.com/rjbuckley332/JamBetterBase.git`
- **Deployed branch in current flow:** `master`
- **Local repo path on deployment host:** `/home/nds`

> Important: This workflow is **Git-first**. For bad app changes, use Git rollback deploy.
> Snapshot rollback is for machine-level failure/drift.

---

## Deployment Host

- Central deployment host runs orchestrator scripts and targets remote servers over SSH.
- Current script directory: `/home/nds/deploy`

Key scripts:
- `/home/nds/deploy/deploy_fleet.sh`
- `/home/nds/deploy/canary_rollout.py`
- `/home/nds/deploy/rollback_server.sh`

Inventory:
- Primary: `/home/nds/deploy/fleet_inventory.json`
- Test-only: `/home/nds/deploy/fleet_inventory.testonly.json`
- Template: `/home/nds/deploy/fleet_inventory.template.json`

---

## Current Target Model

- Deployment host is **not** in inventory.
- Inventory currently starts with test server as canary:
  - `jambetter-test` (`canary: true`)
- Add production/customer targets (e.g. `boysgetold`) as `canary: false`.

---

## Standard Deploy (Code-Only, No Reprovision)

### Command
```bash
/home/nds/deploy/deploy_fleet.sh master /home/nds/deploy/fleet_inventory.json 5 0
```

### What happens per target server
1. SSH to server.
2. Write server-specific Caddy config from inventory `fqdn`.
3. Apply env overrides (e.g. `LIBRARY_S3_BUCKET`, `TRACKBOT_RCLONE_REMOTE`).
4. Git sync code (current implementation uses hard reset):
   - `git fetch --tags origin`
   - `git reset --hard origin/master`
5. Restart services:
   - `jamulus-headless.service`
   - `trackbot-web.service`
   - `jamulus-toggle-webapp.service`
   - `jamulus-uploader.service`
6. Health-gate via `health_url` (`/ops/health`).

### Dry-run
```bash
/home/nds/deploy/deploy_fleet.sh master /home/nds/deploy/fleet_inventory.json 5 1
```

---

## Fast Code Rollback (Preferred)

Use when app/config code is bad but host is healthy.

### Option A: Revert commit in Git, then redeploy
```bash
cd /home/nds
git revert <bad_commit_sha>
git push origin master
/home/nds/deploy/deploy_fleet.sh master /home/nds/deploy/fleet_inventory.json 5 0
```

### Option B: Deploy known-good ref directly
If you have a known good commit/tag:
```bash
/home/nds/deploy/deploy_fleet.sh <good_ref> /home/nds/deploy/fleet_inventory.json 5 0
```

> This keeps infrastructure intact and only changes code/runtime state.

---

## Full Server Rollback (Snapshot)

Use only when host is broken (OS drift, unrecoverable service state, failed config baseline).

### Command
```bash
/home/nds/deploy/rollback_server.sh <instance> <snapshot> <static_ip_name> <fqdn> [region]
```

### Example
```bash
/home/nds/deploy/rollback_server.sh boysgetold PipeDreamers4G-golden8 boysgetold-ip boysgetold.jambetter.music us-east-1
```

### What it does
1. Validates snapshot exists.
2. Deletes existing instance (if present).
3. Recreates instance from snapshot.
4. Opens required ports (22/80/443 TCP, 22124 UDP).
5. Reattaches static IP.
6. Updates DNS A record via Cloudflare script.

---

## Required Secrets/Env

Cloudflare DNS upsert script expects:
- `CF_ZONE_ID`
- token variable mapped to `CF_API_TOKEN` (or `CF_DNS_API_TOKEN` mapped in command)

S3/library vars are per-server in inventory (`env_overrides`, `trackbot_rclone_remote`, `s3_bucket`, etc).

---

## Verification Checklist

### Health
```bash
curl -k -sS https://<fqdn>/ops/health | jq .
```

### DNS
```bash
dig +short A <fqdn>
```

### Service status (on target)
```bash
sudo systemctl status caddy jamulus-toggle-webapp jamulus-headless trackbot-web jamulus-uploader --no-pager
```

### Logs
```bash
sudo journalctl -u caddy -u jamulus-toggle-webapp -n 200 --no-pager
```

---

## Recommended Promotion Path

1. Deploy to `jambetter-test` (canary=true) and verify.
2. Add/enable next target in inventory.
3. Deploy fleet in small batch size.
4. If failure:
   - first try Git rollback deploy,
   - snapshot rollback only if host-level failure.

---

## Operational Notes

- Current Caddy ACME mode may be production or staging depending on deploy settings/scripts.
- If cert trust issues appear during test loops, use staging CA intentionally and do not treat TLS warning as app failure.
- Keep deployment host out of target inventory to avoid self-deploy risk.
