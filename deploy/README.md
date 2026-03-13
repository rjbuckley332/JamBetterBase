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

## DNS upsert (optional, but recommended)

`canary_rollout.py` will **best-effort** upsert a Cloudflare A record for each server *before* deploying,
so first-time deployments don't fail due to missing DNS.

It runs only when:
- the server has a `fqdn` (or a `health_url` from which a hostname can be derived), AND
- `CF_API_TOKEN` is set in the environment.

Env vars:
```bash
export CF_API_TOKEN="<token-with-DNS-edit-permission>"
# Optional (defaults to jambetter.music)
export CF_ZONE_NAME="jambetter.music"
# Optional (auto-discovered if Zone:Read is permitted)
export CF_ZONE_ID="<cloudflare-zone-id>"
```

Inventory fields:
- `fqdn` (recommended) — the full hostname to upsert
- `ssh_host` — used as the IPv4 target for the A record
- `dns_proxied` (optional, default false) — whether Cloudflare proxy should be enabled

Notes:
- If `CF_API_TOKEN` is not set, DNS upsert is skipped.
- If DNS upsert fails, rollout stops (to avoid deploying to a host that operators can't reach by name).
