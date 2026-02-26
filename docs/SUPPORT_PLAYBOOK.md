# JamBetter Support Playbook (v1)

## Purpose
Give fast, actionable support responses for common customer issues, with clear escalation rules.

## Response Style (bot)
- Short, concrete steps (3–6 bullets max).
- Ask for one diagnostic at a time.
- Prefer deterministic checks over generic advice.
- If unresolved after 2 guided attempts, escalate.

---

## Severity / Escalation Rules
Escalate immediately if any of these are true:
- Server unreachable in ops dashboard for >5 minutes.
- Multiple users report severe delay/clicking simultaneously.
- Recording data appears missing across multiple sessions.
- TLS/DNS outage for customer production hostname.
- Any sign of data loss or security concern.

Escalate after 2 failed attempts for non-critical issues.

Escalation payload must include:
- customer/server id
- user symptom summary
- timestamps/timezone
- actions already tried
- key command/API outputs

---

## Top Issue Flows

### 1) Can't connect to server
Checks:
1. Confirm hostname and port (22124 UDP) in client.
2. Verify ops health endpoint (`/ops/health`) reachable.
3. Verify DNS A record resolves to expected static IP.
4. Check server process active (`jamulus-headless`).

Fixes:
- Correct hostname/port.
- Restart `jamulus-headless` if down.
- Reattach static IP + DNS upsert if mismatch.

Escalate if still failing after restart + DNS verified.

---

### 2) Audio delay too high
Checks:
1. Ping + jitter quality in Jamulus client.
2. Current JACK period on server (2048/3072/4096).
3. User on wired vs Wi‑Fi.

Fixes:
- Prefer wired network.
- Keep stable JACK setting (default 4096 if clicks at lower values).
- Reduce competing background traffic.

Escalate if multiple users impacted in same time window.

---

### 3) Clicking/crackling audio
Checks:
1. Is backing track active?
2. CPU/load spikes?
3. JACK period currently set.

Fixes:
- Raise JACK period to 4096.
- Pause heavy post-processing during session.
- Confirm interface sample rate consistency.

Escalate if clicks persist at stable baseline config.

---

### 4) My voice too loud/quiet
Checks:
1. Jamulus Input Boost (should generally be None).
2. Interface/OS input gain and AGC disabled.
3. Confirm user actually reconnects after gain change.

Fixes:
- Adjust hardware/OS gain first.
- Disable auto gain/smart gain in interface software.

Escalate if level appears locked despite gain changes.

---

### 5) Recording missing
Checks:
1. Confirm recording stop happened.
2. Check uploader service status/logs.
3. Check latest `Jam-*` folder and `.uploaded/*.done` marker.
4. Check library listing API for date/session path.

Fixes:
- Restart uploader if down.
- Resolve uploader script/runtime errors.
- Wait settle window (default ~45s+) then recheck.

Escalate if no folder + no uploader activity after restart.

---

### 6) Recording split into multiple sessions
Checks:
1. Multiple `Jam-*` folders for close timestamps.
2. User stop/start or reconnect events.

Fixes:
- Explain chunk behavior (per Jam folder).
- Offer post-session merge workflow if needed.

Escalate only if splitting happens with no user/network events.

---

### 7) Backing tracks show wrong customer data
Checks:
1. `TRACKBOT_RCLONE_REMOTE`
2. `LIBRARY_S3_BUCKET`
3. `LIBRARY_VPS_ID`

Fixes:
- Correct env vars per customer.
- Restart `trackbot-web` + `jamulus-toggle-webapp`.

Escalate if values correct but data still cross-tenant.

---

### 8) Ops dashboard red X but app works
Checks:
1. Health endpoint with strict TLS vs insecure test.
2. Cert chain trust (staging certs show untrusted).

Fixes:
- For production, use trusted ACME cert.
- Do not disable TLS verification permanently.

Escalate if trusted cert still fails validation.

---

### 9) DNS mismatch / slow propagation
Checks:
1. Cloudflare read-back record.
2. Public resolver checks (`@1.1.1.1`, `@8.8.8.8`).
3. Local resolver cache state.

Fixes:
- Re-run DNS upsert.
- Flush local DNS cache if needed.

Escalate if read-back correct but public resolvers stale >15 min.

---

### 10) Deploy failed
Checks:
1. Canary step failure point (SSH/auth/caddy/health).
2. Inventory entry correctness.
3. Service logs on target.

Fixes:
- Correct inventory/env and rerun canary.
- If broken state, rollback to known-good snapshot.

Escalate if rollback also fails.

---

## Suggested Bot Intents
- `status`
- `connectivity`
- `latency`
- `clicking`
- `gain`
- `recording_missing`
- `recording_split`
- `backing_track_wrong_data`
- `dns_cert`
- `deploy_failure`
- `escalate`

---

## Human Handoff Template
"Escalation: <customer/server>. Symptom: <brief>. Time: <tz timestamp>. Tried: <steps>. Current state: <key outputs>. Requested action: <what operator should do next>."
