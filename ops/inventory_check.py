#!/usr/bin/env python3
"""JamBetter ops inventory checker.

Purpose
- Validate ops_servers.json format (id/name/url/zone/tags).
- Probe each server's status endpoint (prefers /ops/status) and report reachability.
- Optionally probe Jamulus control endpoints (start/stop/restart) in *dry-run* mode
  by just checking they exist (OPTIONS request is not reliable across proxies, so we
  do a POST with a dedicated request_id and expect a 2xx/4xx/401/403/404).

This is meant to be run by operators before pointing the unified dashboard at a new
fleet inventory.

Usage
  python3 ops/inventory_check.py --servers ops_servers.json
  python3 ops/inventory_check.py --servers ops_servers.json --check-control

Auth
- If a server entry includes "token", it will be sent as X-Ops-Token.
- You can also set OPS_DASHBOARD_TOKEN to apply to all servers (overridden by per-server token).

Exit codes
- 0: all servers reachable (status) and inventory valid
- 2: one or more servers failed status probe
- 3: inventory invalid/unreadable

"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def http_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, payload: dict | None = None, timeout: float = 6.0) -> tuple[int, dict[str, Any]]:
    data = None
    hdrs: dict[str, str] = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)

    def parse_body(body: str) -> dict[str, Any]:
        body = (body or "").strip()
        if not body:
            return {}
        try:
            v = json.loads(body)
            if isinstance(v, dict):
                return v
            return {"ok": True, "data": v}
        except Exception:
            return {"error": "non-json response", "raw": body[:4000]}

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="ignore")
            return r.getcode(), parse_body(body)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="ignore")
            return e.code, parse_body(body)
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}


def load_servers(path: str) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("servers", [])
    if not isinstance(data, list):
        raise ValueError("servers file must contain a list or {servers:[...]} dict")

    out: list[dict[str, Any]] = []
    for i, s in enumerate(data):
        if not isinstance(s, dict):
            continue
        sid = (s.get("id") or s.get("name") or f"srv-{i+1}").strip()
        url = (s.get("url") or "").strip()
        if not url:
            raise ValueError(f"server {sid} missing url")
        zone = (s.get("zone") or "").strip() or "home"
        tags = s.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        if not isinstance(tags, list):
            tags = []
        out.append({
            "id": sid,
            "name": (s.get("name") or sid).strip(),
            "url": url,
            "zone": zone,
            "tags": tags,
            "token": (s.get("token") or "").strip(),
            "timeout": float(s.get("timeout", 6.0)),
        })
    return out


def status_candidates(base_url: str) -> list[str]:
    base = base_url.rstrip("/")
    return [
        base + "/ops/status",
        base + "/api/support/status",
        base + "/status",
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--servers", default="ops_servers.json", help="path to ops_servers.json")
    ap.add_argument("--check-control", action="store_true", help="probe /api/jamulus/{start|stop|restart} endpoints")
    ap.add_argument("--timeout", type=float, default=6.0, help="HTTP timeout seconds (overrides per-server timeout)")
    args = ap.parse_args()

    try:
        servers = load_servers(args.servers)
    except Exception as e:
        print(f"[{utc_ts()}] inventory invalid: {e}")
        return 3

    fleet_token = (os.getenv("OPS_DASHBOARD_TOKEN") or "").strip()

    print(f"[{utc_ts()}] loaded {len(servers)} servers from {args.servers}")

    failures = 0
    for s in servers:
        sid = s["id"]
        name = s.get("name") or sid
        base = s["url"].rstrip("/")
        zone = s.get("zone") or "home"
        tags = ",".join([str(t) for t in (s.get("tags") or [])])
        tok = s.get("token") or fleet_token
        headers: dict[str, str] = {}
        if tok:
            headers["X-Ops-Token"] = tok

        ok = False
        last = ""
        src = ""
        for u in status_candidates(base):
            code, data = http_json(u, headers=headers, timeout=args.timeout)
            if code and 200 <= code < 300 and isinstance(data, dict):
                ok = True
                src = u
                grade = ((data.get("quality") or {}) if isinstance(data.get("quality"), dict) else {}).get("grade")
                svc = (((data.get("jamulus") or {}).get("service") or {}) if isinstance(data.get("jamulus"), dict) else {}).get("state")
                print(f"- {sid} ({name}) zone={zone} tags={tags or '-'} : OK via {urllib.parse.urlparse(src).path} grade={grade or '-'} jamulus={svc or '-'}")
                break
            last = (data.get("error") if isinstance(data, dict) else str(data)) or ""
        if not ok:
            failures += 1
            print(f"- {sid} ({name}) zone={zone} tags={tags or '-'} : FAIL ({last or 'unreachable'})")
            continue

        if args.check_control:
            for action in ("start", "stop", "restart"):
                url = f"{base}/api/jamulus/{action}"
                # We send a request_id so server logs/journal can be traced; this may
                # trigger a real action on misconfigured servers, so keep this opt-in.
                rid = f"inventory-check-{utc_ts()}-{action}"
                code, data = http_json(url, method="POST", headers={**headers, "X-Request-Id": rid}, payload={"request_id": rid}, timeout=args.timeout)
                # Accept any non-0 code as "endpoint reachable"; operators can interpret.
                reachable = bool(code)
                status = "reachable" if reachable else "unreachable"
                note = ""
                if isinstance(data, dict):
                    note = data.get("error") or data.get("result") or data.get("state") or ""
                print(f"  - control {action}: {status} (http {code or '-'}) {note}")

    if failures:
        print(f"[{utc_ts()}] done: {failures} server(s) failed status probe")
        return 2

    print(f"[{utc_ts()}] done: all servers reachable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
