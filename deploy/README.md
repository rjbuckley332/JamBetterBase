# JamBetter Fleet Deploy (Canary v1)

## Files
- `fleet_inventory.json` — server inventory for rollout
- `canary_rollout.py` — canary-first rolling deploy orchestrator

## Inventory format
```json
{
  "repo": "/home/nds",
  "services": [
    "jamulus-headless.service",
    "trackbot-web.service",
    "jamulus-toggle-webapp.service",
    "jamulus-uploader.service"
  ],
  "servers": [
    {
      "id": "pipedreamers",
      "name": "pipedreamers",
      "ssh_host": "13.221.152.126",
      "ssh_user": "ubuntu",
      "health_url": "https://pipedreamers.jambetter.music/ops/health",
      "ops_token": "",
      "canary": true
    }
  ]
}
```

## Dry run
```bash
/home/nds/deploy/canary_rollout.py v2026.02.23-1 /home/nds/deploy/fleet_inventory.json 5 1
```

## Real deploy
```bash
/home/nds/deploy/canary_rollout.py v2026.02.23-1 /home/nds/deploy/fleet_inventory.json 5 0
```

Behavior:
1. Deploy canary first
2. Health-gate canary
3. Roll out remaining servers in batches
4. Stop immediately on failure
