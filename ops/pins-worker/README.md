# JamBetter Ops Pins Service (Cloudflare Worker + D1)

This directory contains a minimal **remote pins backend** for the unified ops dashboard.

The dashboard (`ops_dashboard_app.py`) can be configured to proxy pin reads/writes to this service:

- `OPS_PINS_REMOTE_URL=https://<your-worker-host>`
- `OPS_PINS_REMOTE_TOKEN=<token>` (sent as `X-Ops-Token`)

## API

The worker implements the same pins API shape the dashboard expects:

- `GET    /api/pins` → `{ ok, ts, pins: [...] }`
- `POST   /api/pins` body `{ title?, body? }` → `{ ok, id }`
- `POST   /api/pins/:id/pin` body `{ pinned: true|false }` → `{ ok }`
- `DELETE /api/pins/:id` → `{ ok }`

Pins are sorted pinned-first then newest-first.

## Auth

Set `OPS_PINS_TOKEN` (Worker env var).

Requests must include:
- `X-Ops-Token: <OPS_PINS_TOKEN>`

## D1 schema

Apply `schema.sql` to your D1 database.

## Deploy (example)

```bash
cd ops/pins-worker
npm i
npx wrangler d1 create jambetter-pins
# update wrangler.toml with the DB id
npx wrangler d1 execute jambetter-pins --file=./schema.sql
npx wrangler deploy
```

Then set `OPS_PINS_REMOTE_URL` and `OPS_PINS_REMOTE_TOKEN` on the dashboard host.
