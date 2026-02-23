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

## Cloudflare DNS upsert (required for new customer provisioning)

Set env on central ops server:

```bash
export CF_API_TOKEN="<token-with-DNS-edit-permission>"
export CF_ZONE_ID="<cloudflare-zone-id-for-jambetter.music>"
export CF_ZONE_NAME="jambetter.music"
```

Create/update customer record:

```bash
/home/nds/deploy/cloudflare_dns_upsert.py boysgetold 203.0.113.10 false
```

- Last argument (`false`) means **DNS only** (recommended until cert is stable).
- Use `true` only when you intentionally want Cloudflare proxy.

## DNS is now required in rollout

 now performs Cloudflare DNS upsert for each server before deploy.
Set these env vars in the shell running rollout:



Inventory may optionally include ; otherwise  is used as DNS target.
