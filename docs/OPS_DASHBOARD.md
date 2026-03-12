# JamBetter Unified Ops Dashboard

This repo contains a lightweight **unified ops dashboard** for monitoring and controlling a JamBetter fleet.

The dashboard is implemented in `ops_dashboard_app.py` (Flask) and is meant to run on an operator box (or any host with network access to your JamBetter servers).

## What it does (MVP)

Tabs:
- **Fleet**: inventory + status rollup (polls each server)
- **Pins**: lightweight operator notes (file-backed for now)
- **Health**: runs `healthcheck.py` and renders `/tmp/jambetter_health.json`

Primary API surface:
- `GET /` → HTML dashboard
- `GET /api/servers` → server inventory
- `GET /api/fleet` → fleet status rollup (polls each server in parallel)
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
      "token": "<optional per-server token>",
      "timeout": 4
    }
  ]
}
```

Notes:
- `url` should be the **base URL** of the JamBetter server (no trailing path).
- The fleet poller probes these status endpoints (first one that works):
  - `/api/support/status` (preferred)
  - `/ops/status`
  - `/status`
  - `/` (last resort)

### Pins storage

Pins are file-backed (JSON) for now.

- `OPS_PINS_FILE=/tmp/jambetter_ops_pins.json` (default)

### Health check

- `JAMBETTER_HEALTH_SCRIPT=/home/nds/healthcheck.py` (default)
- `JAMBETTER_HEALTH_JSON=/tmp/jambetter_health.json` (default)

## Running locally

```bash
export OPS_DASHBOARD_PORT=5090
python3 ops_dashboard_app.py
```

Then open:
- `http://127.0.0.1:5090/`

If you set a token:
- `http://127.0.0.1:5090/?token=YOUR_TOKEN`

## Server-side requirements (control plane)

The Fleet tab’s **Start/Stop/Restart** buttons call:
- `POST <serverBaseUrl>/api/jamulus/start`
- `POST <serverBaseUrl>/api/jamulus/stop`
- `POST <serverBaseUrl>/api/jamulus/restart`

Those endpoints must exist on each server (or you’ll see upstream 404/502 errors).

---

Next likely steps:
- Replace file-backed pins with a real DB (e.g., Cloudflare D1 or SQLite)
- Add per-action audit log
- Add caching + last-seen timestamps to reduce poll load
