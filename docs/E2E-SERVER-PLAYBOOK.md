# JamBetter End-to-End Server Creation Playbook

**Version:** 1.0 — 2026-03-24
**Owner:** Ops (Rich / JB)

---

## Overview

This playbook covers the full lifecycle from customer sign-up to a working JamBetter server, including what's automated, what's manual, and where the gaps are.

```
Customer clicks    Stripe processes    Webhook fires    Server provisions    DNS set    Welcome sent
"Get my server" → checkout.session → stripe-webhook.js → provision script → Cloudflare → Resend email
     ↓                  ↓                   ↓                  ↓               ↓            ↓
  Website         Payment Link         CF Pages Function    GCP + SSH       A record     Customer
  (index.html)    (buy.stripe.com)     (D1 + dispatch)      (bash script)   (DNS-only)   ready!
```

---

## Phase 1: Website Sign-Up

### What happens
1. Customer visits `www.jambetter.music`
2. Clicks **"Get my server"** button
3. Redirected to Stripe Payment Link (hosted checkout)

### Payment Link IDs
| Product | Link ID | Triggers provisioning? |
|---------|---------|----------------------|
| Discount server | `plink_1TDkgu…JJLL` | ✅ Yes |
| Managed server | `plink_1T3wg1…6i1S` | ✅ Yes |
| Rig (hardware) | `plink_1TCe7O…WmEQ` | ❌ No (notification only) |

### Where it lives
- `marketing/index.html` — button links
- Active discount link: `https://buy.stripe.com/4gM9AS4ZJeiH7om2sVeIw05`

### Checklist
- [ ] Payment link is live and correct in `index.html`
- [ ] Stripe product/price configured correctly
- [ ] Checkout collects customer email

---

## Phase 2: Stripe Payment Processing

### What happens
1. Customer completes payment on Stripe
2. Stripe fires `checkout.session.completed` webhook
3. Webhook hits Cloudflare Pages Function (`stripe-webhook.js`)

### Webhook routing logic
```
checkout.session.completed
  ├── payment_link matches Rig? → rig-order notification only
  ├── payment_link matches Server? → proceed to provisioning
  └── payment_link unknown? → PAID_UNROUTED alert (safe default, no provision)
```

### Webhook security
- Verifies Stripe signature via `STRIPE_WEBHOOK_SECRET`
- Persists event to D1 database `jambetter_booking`

### D1 tables involved
- `stripe_events` — raw event log
- `stripe_checkout_status` — checkout state tracking
- `provision_jobs` — provisioning job queue
- `slug_reservations` — concurrency-safe slug claims

### Slug reservation (concurrency-safe)
- Sanitizes desired slug from checkout metadata
- D1 insert with DB-enforced uniqueness on `resolved_slug`
- First claimant gets base slug (e.g., `myband`)
- Concurrent claimants get suffixed (`myband-2`, `myband-3`)
- Same checkout session retries reuse existing reservation (idempotent)

### Checklist
- [ ] `STRIPE_WEBHOOK_SECRET` set in CF Pages env
- [ ] Webhook endpoint registered in Stripe dashboard
- [ ] D1 database `jambetter_booking` accessible
- [ ] Test: create test checkout → verify D1 records appear

---

## Phase 3: Provision Dispatch

### What happens
1. Webhook POSTs dispatch payload to `PROVISION_DISPATCH_URL`
2. `provision-dispatch-server.js` on host validates token, returns 202
3. Asynchronously kicks off `provision-jambetter-server.sh`

### Dispatch payload fields
- `job_id`, `instance_name`, `fqdn`, `group_slug`
- `source`, `zone`, `zone_candidates`
- `welcome_email`

### Dispatch endpoint
- **Current:** `http://13.221.152.126:8790/provision` (⚠️ raw IP, HTTP)
- **Needed:** Stable HTTPS endpoint (e.g., `provision.jambetter.music`) behind reverse proxy
- Auth: `x-provision-token` header

### Checklist
- [ ] `provision-dispatch-server.js` running on ops host
- [ ] `PROVISION_DISPATCH_URL` set in CF Pages env
- [ ] `x-provision-token` matches between webhook and dispatch server
- [ ] Test: manual POST to dispatch endpoint → verify 202

---

## Phase 4: Server Creation (GCP)

### What happens (inside `provision-jambetter-server.sh`)

**Step-by-step:**

| Step | Action | Details |
|------|--------|---------|
| 1 | Remove old DNS | Delete existing A records for FQDN in Cloudflare |
| 2 | Delete old instance | Only if `--recreate` flag set |
| 3 | Create machine image | Snapshot from source instance (e.g., `seiger`) |
| 4 | Create GCP instance | From machine image, with zone fallback + static IP |
| 5 | Wait for SSH | Up to 30 attempts, 5s apart |
| 6 | Configure Caddy | Write Caddyfile with customer FQDN, reload |
| 7 | Deploy SSH key | OpenClaw deploy key for `nds` user, enable SSH login |
| 8 | Configure S3 wiring | Set `TRACKBOT_RCLONE_REMOTE`, `LIBRARY_S3_BUCKET`, `LIBRARY_VPS_ID` |
| 9 | Auto-shutdown watchdog | Configure idle-shutdown (default: 1hr idle → stop) |
| 10 | Create DNS A records | Cloudflare A record → static IP (DNS-only, no proxy) |
| 11 | Booking mapping | Create booking credentials via admin API |
| 10.5 | Support PIN | Generate 6-digit PIN with 5-day active window |
| 12 | Welcome email | Send via Resend API |

### Required environment
```bash
CF_PAGES_TOKEN or CF_DNS_TOKEN   # Cloudflare DNS management
CF_ZONE_ID                       # Cloudflare zone
GCP_SA_KEY_FILE                  # GCP service account (default: /home/nds/deploy/gcp-sa.json)
GCP_PROJECT                      # GCP project (default: jambetter)
BOOK_ADMIN_TOKEN                 # Booking admin API token
RESEND_API_KEY                   # Welcome email sending
```

### Example invocation
```bash
./provision-jambetter-server.sh \
  --name myband \
  --fqdn myband.jambetter.music \
  --source seiger \
  --zone us-east4-a \
  --book-group-slug myband \
  --book-group-pin 123456 \
  --welcome-email customer@example.com
```

### S3 bucket creation
- Auto-creates bucket `<name>-recordings-prod` in `us-east-2`
- Sets prefix `vps/vps-vm-<name>`

### Checklist
- [ ] GCP service account key present and valid
- [ ] Source instance (`seiger`) healthy and up-to-date
- [ ] Cloudflare tokens available (see `~/.cloudflare_tokens`)
- [ ] Booking admin token in `~/.openclaw/.secrets/book_admin_token`
- [ ] Resend API key in `~/.openclaw/.secrets/env.sh`
- [ ] SSH deploy key pair at `/home/nds/.ssh/openclaw_deploy_ed25519[.pub]`
- [ ] AWS credentials configured for S3 bucket creation

---

## Phase 5: DNS Setup

### What happens
- Cloudflare A record: `<slug>.jambetter.music` → GCP static IP
- **DNS-only** (no Cloudflare proxy — Jamulus uses UDP, can't proxy)
- Caddy handles HTTPS/TLS on ports 80/443 for web UI
- TTL: 120 seconds

### Important: tenant identity is stable
- Customer DNS (`<slug>.jambetter.music`) stays the same even if workload moves between hosts
- Infrastructure hostnames can change; customer-facing DNS doesn't

### Checklist
- [ ] A record created and resolving (`dig <slug>.jambetter.music`)
- [ ] Caddy serving HTTPS with valid TLS cert
- [ ] `https://<slug>.jambetter.music` returns 200

---

## Phase 6: Booking & Support PIN

### What happens
1. Booking mapping created via admin API (`/api/admin/upsert-mapping`)
   - Maps `groupSlug` → GCP project/zone/instance
   - Creates login credentials (username + PIN)
2. Support PIN generated (6-digit, 5-day window in America/New_York)
   - Created via `/api/admin/upsert-support-pin`

### Checklist
- [ ] Booking API returns 200 for mapping upsert
- [ ] Customer can log in at `https://www.jambetter.music/book/`
- [ ] "Start Now" button works and instance starts
- [ ] Support PIN valid and window correct

---

## Phase 7: Welcome Email

### What happens
- Sent via **Resend API** to customer email
- Script: `send-welcome-email.sh`

### Email contains
- Server URL and Jamulus host:port
- Booking portal URL + credentials
- Auto-sleep explanation
- Phone support info (if support PIN set)
  - Phone number: `+18338530529`
  - 6-digit PIN
  - Active window dates
- Quick-start instructions (4 steps)

### Checklist
- [ ] `RESEND_API_KEY` available
- [ ] From address: `no-reply@jambetter.music`
- [ ] Email delivered (check Resend dashboard)
- [ ] All fields populated correctly

---

## Phase 8: Post-Provisioning Verification

### Automated health checks (in provision script)
- Services active: `jamulus-headless`, `jamulus-injector`, `jamulus-toggle-webapp`, `jamulus-uploader`
- S3 env vars correct in `/home/nds/.env`
- Auto-shutdown watchdog running (if enabled)

### Manual smoke test
- [ ] Connect Jamulus client to `<slug>.jambetter.music:22124`
- [ ] Start/stop metronome
- [ ] Run short recording → verify upload to S3
- [ ] Check web file browser (play + download)
- [ ] Verify ops dashboard shows server healthy

### Ops integration
- [ ] Health endpoint live: `https://<slug>.jambetter.music/ops/health`
- [ ] Server added to `ops_servers.json` on central dashboard
- [ ] Dashboard shows green status

---

## Known Gaps & TODOs

| Gap | Severity | Status |
|-----|----------|--------|
| Dispatch URL is raw IP:8790 (HTTP) | Medium | Needs stable HTTPS endpoint |
| MT apply is manual (`mt-apply-manual.sh`) | Medium | Not auto-triggered by dispatch |
| Refund → deprovision not automated | Medium | D1 status updated but infra not torn down |
| Duplicate webhook files (4 copies) | Low | Risk of drift; needs dedup |
| Automated placement/admission (8-tenant cap) | Low | Policy exists, not coded |
| S3 lifecycle (15-day trash expiry) | Low | Not confirmed on all prefixes |
| `jb-` prefix in instance naming | Low | User wants slug-only; partially done |

---

## Quick Reference: Key Files

| File | Purpose |
|------|---------|
| `provision-jambetter-server.sh` | Main provisioning script |
| `send-welcome-email.sh` | Welcome email via Resend |
| `provision-dispatch-server.js` | HTTP dispatch endpoint (on host) |
| `stripe-webhook.js` | CF Pages Function (webhook handler) |
| `DEPLOYMENT-CHECKLIST.md` | Server config validation checklist |
| `jambetterbase/docs/SUPPORT_PLAYBOOK.md` | Customer support issue flows |
| `mt/scripts/mt-apply-manual.sh` | Multi-tenant service provisioning |

---

## Quick Reference: Key Secrets

| Secret | Location | Used by |
|--------|----------|---------|
| `STRIPE_WEBHOOK_SECRET` | CF Pages env | `stripe-webhook.js` |
| `CF_DNS_TOKEN` / `CF_PAGES_TOKEN` | `~/.cloudflare_tokens` | Provision script |
| `CF_ZONE_ID` | `~/.cloudflare_tokens` | Provision script |
| `GCP_SA_KEY_FILE` | `/home/nds/deploy/gcp-sa.json` | Provision script |
| `BOOK_ADMIN_TOKEN` | `~/.openclaw/.secrets/book_admin_token` | Provision script |
| `RESEND_API_KEY` | `~/.openclaw/.secrets/env.sh` | Welcome email |
| `x-provision-token` | CF Pages env + dispatch server | Dispatch auth |

---

## Rollback / Recovery

### If provisioning fails mid-way:
1. Check which step failed (script prints `==> [N/M]` markers)
2. Fix the issue
3. Re-run with `--recreate` to start fresh, or fix in-place and continue

### If customer reports issues after provisioning:
1. Follow `SUPPORT_PLAYBOOK.md` for standard issue flows
2. Verify all checklist items in `DEPLOYMENT-CHECKLIST.md`
3. Check ops dashboard for health status

### Refund / nonpayment path (manual classification first)
1. Receive Stripe refund / cancellation / nonpayment signal
2. Identify the customer slug from Stripe metadata / checkout / booking records
3. **Classify the slug before deleting anything:**
   - Search for the slug/hostname and determine whether it is a **dedicated server** or a **shared tenant on an MT host**
   - Confirm the exact live hostname (for example `testing.jambetter.music` vs `jb-testing.jambetter.music`)
4. Branch by hosting model:
   - **Dedicated server:** full teardown path
     - remove booking mapping / support PIN
     - remove Cloudflare DNS record(s)
     - stop and delete the dedicated GCP instance/server
     - remove any dedicated storage/config artifacts tied to that server
   - **Shared tenant:** tenant-only teardown path
     - if it is **not the last tenant on the host**, delete only that slug's tenant components
       - tenant service units/config
       - tenant temp/storage paths
       - tenant routing/DNS/booking records
     - if it **is the last tenant**, pause and evaluate whether the whole host should also be retired instead of doing a partial cleanup
5. Verify D1 / webhook state reflects the refund/nonpayment
6. Confirm DNS, booking, and runtime cleanup are complete
7. **TODO:** automate this classification + teardown flow safely
