# JamBetter Unified Ops Dashboard

This repo contains a lightweight **unified ops dashboard** for monitoring and controlling a JamBetter fleet.

The dashboard is implemented in `ops_dashboard_app.py` (Flask) and is meant to run on an operator box (or any host with network access to your JamBetter servers).

## What it does (MVP)

Tabs:
- **Fleet**: inventory + status rollup (polls each server)
- **Servers**: raw inventory view (shows `GET /api/servers` output)
- **Pins**: lightweight operator notes (file-backed for now)
- **Health**: runs `healthcheck.py` and renders `/tmp/jambetter_health.json`

Primary API surface:
- `GET /` → HTML dashboard
- `GET /api/servers` → server inventory
- `GET /api/fleet` → fleet status rollup (polls each server in parallel)
  - Optional: `GET /api/fleet?zone=home|all|<zone>` to reduce polling to a single zone.
  - Optional: `GET /api/fleet?...&tag=<tag>` to restrict to servers whose inventory tags include `<tag>` (exact match).
- `POST /api/server/<id>/action` body `{ "action": "start|stop|restart" }` → proxies to the server
- `GET /api/pins` → list pins
- `POST /api/pins` body `{ "title": "...", "body": "..." }` → create pin (defaults to pinned=true)
- `POST /api/pins/<pin_id>/pin` body `{ "pinned": true|false }` → pin/unpin
- `DELETE /api/pins/<pin_id>` → delete pin

## Auth

Optional shared token:
- Set `OPS_DASHBOARD_TOKEN`.
- Provide it as either:
  - `?token=...` in the URL, or
  - `X-Ops-Token: ...` header.

If `OPS_DASHBOARD_TOKEN` is unset/empty, the dashboard is open.

## Configuration

### Server inventory

By default the dashboard loads `ops_servers.json` in the repo root.

Templates:
- `ops_servers.template.json` (example multi-zone inventory)

Override with:
- `OPS_SERVERS_FILE=/path/to/ops_servers.json`

File format:

```json
{
  "servers": [
    {
      "id": "vps-0001",
      "name": "pipedreamers",
      "url": "https://pipedreamers.example.com",
      "zone": "home",
      "tags": ["prod"],
      "token": "<optional per-server token>",
      "timeout": 4
    }
  ]
}
```

Notes:
- `url` should be the **base URL** of the JamBetter server (no trailing path).
- `zone` is optional (defaults to `home` if omitted).
  - The Fleet tab’s zone dropdown auto-discovers zones by calling `GET /api/servers`.
- `tags` is optional (array of strings).
- The fleet poller probes these status endpoints (first one that works):
  - `/ops/status` (preferred stable schema)
  - `/api/support/status` (back-compat)
  - `/status`
  - `/` (last resort)

Expected JSON shape (minimum) from a server status endpoint:
```json
{
  "ok": true,
  "server_id": "vps-0001",
  "quality": {"grade": "green|yellow|red", "label": "..."},
  "load": {"load1": 0.12, "cores": 2},
  "disk": {"used_pct": 17.3},
  "jamulus": {"service": {"state": "active|inactive|failed|unknown"}}
}
```

If `jamulus.service.state` is present, the dashboard renders it in the **Jamulus** column.

### Pins storage

Pins are file-backed (JSON) by default.

- `OPS_PINS_FILE=/tmp/jambetter_ops_pins.json` (default)

Optional remote backend (e.g. Cloudflare Worker backed by D1):

- `OPS_PINS_REMOTE_URL=https://your-pins-service.example.com` (when set, pins are proxied to this service)
- `OPS_PINS_REMOTE_TOKEN=...` (sent as `X-Ops-Token` to the remote pins service)

The remote service is expected to expose the same endpoints as this dashboard:
- `GET/POST  /api/pins`
- `POST     /api/pins/<pin_id>/pin`
- `DELETE   /api/pins/<pin_id>`

### Fleet polling cache

If multiple operators open the dashboard, the `GET /api/fleet` endpoint can cause a burst of polls.

UI notes:
- Fleet table shows `ts` (last seen) and marks servers as **stale** when the timestamp is older than ~5 minutes.
- Action buttons apply a short cooldown (~8s) after an action to reduce accidental repeat clicks.

- `OPS_FLEET_CACHE_S=5` (default) caches the fleet response in-memory for N seconds (cache is keyed by `?zone=`).
- Set to `0` to disable caching.

### Audit log

Action proxy calls (start/stop/restart) are logged best-effort to a local append-only JSONL file.

- `OPS_AUDIT_FILE=/tmp/jambetter_ops_audit.jsonl` (default)

Additional endpoints:
- `GET /api/audit?limit=200` → `{ ok, ts, audit: [...] }` (tail of the JSONL log)
- `GET /api/export` → `{ ok, ts, pins: [...], audit: [...] }` (simple incident-review export)

Operator identity (optional):
- Fleet tab includes an **Operator** field (stored in localStorage).
- When set, it’s sent as `X-Operator-Id` on action requests and logged as `operator_id` in the audit file.


### Health check

- `JAMBETTER_HEALTH_SCRIPT=/home/nds/healthcheck.py` (default)
- `JAMBETTER_HEALTH_JSON=/tmp/jambetter_health.json` (default)

## Inventory validation (preflight)

Before pointing the dashboard at a new or edited `ops_servers.json`, you can run a quick preflight checker:

```bash
python3 ops/inventory_check.py --servers ops_servers.json
# optional (may trigger real actions on misconfigured servers):
python3 ops/inventory_check.py --servers ops_servers.json --check-control
```

It validates the inventory format and probes each server’s status endpoint (prefers `/ops/status`).

## Running locally

```bash
export OPS_DASHBOARD_PORT=5090
python3 ops_dashboard_app.py
```

Then open:
- `http://127.0.0.1:5090/`

If you set a token:
- `http://127.0.0.1:5090/?token=YOUR_TOKEN`

## Running as a service (systemd template)

See:
- `deploy/ops-dashboard.service.template`
- `deploy/ops-dashboard.env.example`

These are templates meant to be copied to your target host and adjusted for:
- the repo path (where `ops_dashboard_app.py` lives)
- the user/group to run as
- the inventory path (`OPS_SERVERS_FILE`)
- pins/audit/health paths

## Server-side requirements (control plane)

The Fleet tab’s **Start/Stop/Restart** buttons call:
- `POST <serverBaseUrl>/api/jamulus/start`
- `POST <serverBaseUrl>/api/jamulus/stop`
- `POST <serverBaseUrl>/api/jamulus/restart`

Those endpoints must exist on each server (or you’ll see upstream 404/502 errors).

Notes:
- The dashboard forwards `X-Request-Id` (if provided) to the server to support idempotency/correlation.
- If the server supports async/long-running orchestration, it can expose:
  - `GET <serverBaseUrl>/api/jamulus/action/<request_id>`
  The dashboard will poll this briefly when an action returns `accepted`/`in_progress`.

---

Home-zone-first policy
- Operate each server primarily within its own home zone.
- Do **not** force cross-zone migration (recordings/data/workloads). Any cross-zone actions must be explicit and operator-initiated.

Rewrite plan
- See `docs/DASHBOARD_CONTROL_PLANE_PLAN.md` for milestones focused on reliability-safe start/stop orchestration and a unified ops dashboard roadmap.

Next likely steps:
- Replace file-backed pins with a real DB (e.g., Cloudflare D1 or SQLite)
- Add per-action audit log
- Add caching + last-seen timestamps to reduce poll load
