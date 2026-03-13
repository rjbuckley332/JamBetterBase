# JamBetter dashboard/control-plane rewrite plan (home-zone-first)

This is a practical plan for evolving the **JamBetter unified ops dashboard** + server control-plane APIs.

**Policy constraints**
- **Home-zone-first:** Every server is operated primarily within its own “home” zone/context.
- **No forced cross-zone migration:** The control plane must not automatically move workloads, data, or recordings between zones.
- Cross-zone work (if ever) must be **explicit, opt-in, and operator-initiated**, with clear UI affordances.

---

## Current state (baseline)

As of the current implementation in this repo:

- Operator dashboard: `ops_dashboard_app.py`
  - Inventory API: `GET /api/servers` (supports `zone` + `tags`)
  - Fleet rollup: `GET /api/fleet?zone=home|all|<zone>&tag=<tag>`
  - Start/stop/restart proxy: `POST /api/server/<id>/action`
  - Per-server drilldown: `GET /server/<id>` (raw JSON payload for debugging)
  - Home-zone-first UX:
    - Default view is `zone=home`
    - Cross-zone visibility is allowed
    - Cross-zone control requires an explicit UI toggle
  - Pins API: file-backed by default, optionally proxied to a remote backend

- Server control plane (on each JamBetter host): `toggle_app.py`
  - Read-only status contract:
    - `GET /ops/status` (preferred stable schema)
    - `GET /api/support/status` (back-compat alias)
    - Includes `schema_version: 1`, `zone`, `quality`, `load`, `disk`, and `jamulus.service.state`
  - Reliability-safe orchestration for Jamulus actions:
    - `POST /api/jamulus/{start|stop|restart}`
    - Idempotency key via `X-Request-Id` / `request_id`
    - Single-action mutex via `fcntl.flock()`
    - Append-only journal for action status
    - `GET /api/jamulus/action/<request_id>` for action result/progress

Primary remaining gaps are mostly “polish + hardening” items (rate limiting, richer drilldown, stronger auth boundaries, and a better shared audit trail).

---

## Target outcomes

1. **Reliability-safe orchestration**
   - Idempotent actions.
   - No overlapping start/stop/restart.
   - Clear result states: accepted → in_progress → done|failed.
   - Safe timeouts and explicit “still working” responses.

2. **Unified ops dashboard milestones**
   - Single fleet view with consistent status schema.
   - Per-server drilldown (optional later) without centralizing the world.
   - Operator notes/audit trail.

3. **Home-zone-first UI/UX**
   - Inventory includes a `zone` field.
   - Default view: your “home zone”.
   - Cross-zone visibility allowed; cross-zone control requires explicit toggle.

---

## Milestones (practical, shippable)

### M1 — Contract: versioned server status schema (read-only)
**Goal:** make polling consistent and useful.

Server must expose `GET /api/support/status` returning:
```json
{
  "ok": true,
  "schema_version": 1,
  "server_id": "vps-0001",
  "zone": "home", 
  "ts": "2026-03-12T18:06:00Z",

  "jamulus": {
    "service": {"state": "active|inactive|failed|unknown", "since": "..."},
    "jsonrpc": {"reachable": true, "port": 22100},
    "recorder": {"recording": true, "session": "..."}
  },

  "quality": {"grade": "green|yellow|red", "label": "..."},
  "load": {"load1": 0.12, "cores": 2},
  "disk": {"used_pct": 17.3}
}
```
Notes:
- `zone` is informational; it does **not** imply migration.
- `schema_version` allows future additive changes without breaking the dashboard.

**Dashboard work:** render jamulus.service state + ts/last-seen.

### M2 — Server-side orchestration: action journal + lock (write path)
**Goal:** make `/api/jamulus/*` reliable and safe.

Implement on each server:
- A single action runner with:
  - **mutex/lock** (file lock or atomic lockfile) to prevent overlap.
  - **idempotency key** support (e.g. `X-Request-Id` header).
  - **journal** persisted locally (e.g. `/tmp/jambetter_actions.json`).

Action semantics:
- `start`
  - If already active → return ok + `result: "noop"`.
- `stop`
  - If already inactive → ok + noop.
- `restart`
  - Treated as stop→start with bounded waits.

Response shape (synchronous MVP):
```json
{ "ok": true, "request_id": "...", "action": "restart", "result": "done|noop", "details": "..." }
```
If long-running, allow:
```json
{ "ok": true, "request_id": "...", "action": "restart", "result": "accepted" }
```
And expose:
- `GET /api/jamulus/action/<request_id>` → returns progress/result.

### M3 — Dashboard: action UX with “in-flight” state
**Goal:** stop double-click/retry chaos.

- Dashboard generates a `request_id` per action.
- Buttons disabled while action in-flight for that server.
- Show last action result + timestamp.
- If upstream returns `accepted`, dashboard polls the action status endpoint briefly (bounded) and then falls back to “pending” display.

### M4 — Home-zone-first enforcement in the dashboard
**Goal:** avoid accidental cross-zone ops.

- Add to `ops_servers.json`:
```json
{ "id": "vps-0001", "name": "...", "url": "...", "zone": "home", "tags": ["prod"] }
```
- Dashboard default filter: `zone=home`.
- Add a UI toggle: “Show all zones”.
- Add a UI toggle: “Enable cross-zone control” (off by default). When off, buttons for non-home zones are disabled.

### M5 — Audit log (operator-friendly)
**Goal:** explain what happened without SSH.

- Append-only action log:
  - who/what/when (best-effort): request_id, server_id, action, result, ts, upstream_code.
- This can be file-backed initially; later move to SQLite/D1.

---

## Near-term TODO list (next work session)

Already completed in this repo:
- Inventory supports `zone` + `tags`.
- Fleet table renders `zone`, `tags`, and `jamulus.service.state`.
- Server-side action journal + `fcntl.flock()` mutex + idempotency key is implemented.
- Docs include home-zone-first UX + schema notes.

Next practical items (remaining):
1. **Tighten auth boundaries**: decide whether the dashboard token is sufficient, or if per-operator auth is needed.
2. **Harden action proxy**: optional server-side rate limiting + clearer upstream error surfacing (e.g. show 401/403 distinctly).
3. **Remote audit** (optional): move audit log from local JSONL to a shared backend (SQLite/D1) for multi-operator visibility.
4. **Richer drilldown**: add structured “server detail” UI (beyond raw JSON) once the status schema stabilizes further.

(Recently completed in this repo: operator id field, per-server cooldown, stale/last-seen highlighting, and export/download for pins + audit.)

---

## Explicit non-goals (for this rewrite)

- No automated migration of recordings/library objects between servers/zones.
- No central “master” that takes ownership of servers.
- No cross-zone failover without explicit operator command + clear UI.

---

## Implementation notes (next practical steps)

### Dashboard (M4) — home-zone-first UX (no forced cross-zone migration)

**Goal:** make cross-zone control *hard to do accidentally*.

Proposed minimal UI additions (fleet tab):
- `HOME_ZONE` (env or constant; default `home`).
- A filter dropdown: `Zone: [home] [all]` (default: home).
- A checkbox toggle: `Enable cross-zone control` (default off).
  - When off, action buttons are disabled for servers whose `zone != HOME_ZONE`.
  - Still allow *visibility* across zones if operator explicitly selects `all`.

Persistence:
- Store operator choices in `localStorage` (`zoneFilter`, `crossZoneControlEnabled`).

Server inventory contract (already supported in `_load_servers()`):
- `ops_servers.json` entries include `zone` and optional `tags`.

Acceptance criteria:
- Fresh load shows only `zone=home` servers.
- Switching to `all` shows other zones, but buttons are disabled until cross-zone control is enabled.

### Control-plane (M2/M3) — reliability-safe start/stop/restart

**Problem in current code:** server endpoints in `toggle_app.py` run `systemctl` directly and return success/failure, with no lock, no idempotency, and no action-in-flight model.

**Minimal design (works with stdlib only):**

1) Add a single action runner with:
- `fcntl.flock()` on a lockfile, e.g. `/tmp/jambetter_jamulus_action.lock`.
- Idempotency key: accept `X-Request-Id` header (or JSON `request_id`).
- Journal file: append JSON lines to `/tmp/jambetter_actions.jsonl`.

2) Add endpoints:
- `POST /api/jamulus/<start|stop|restart>`
  - request: `{ "request_id": "..." }` optional
  - response: `{ ok, request_id, action, result: done|noop|accepted|failed, details, ts }`
- `GET /api/jamulus/action/<request_id>`
  - response includes `state: in_progress|done|failed` and `output`.

3) Idempotency behavior:
- If the same `request_id` is seen again, return the stored result without re-running.

4) "Desired vs actual" (MVP):
- For now, infer actual via `systemctl is-active` and treat desired == action target.
- Later: expose `GET /api/jamulus/state` to simplify dashboard.

Dashboard (M3) UX changes needed later:
- Generate a UUID `request_id` per button press.
- Disable buttons while awaiting completion; if result is `accepted`, display `pending` + allow manual refresh.

