#!/usr/bin/env python3
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse

API_BASE = "https://api.cloudflare.com/client/v4"


def req(method, path, token, body=None):
    url = API_BASE + path
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    if len(sys.argv) < 3:
        print("Usage: cloudflare_dns_upsert.py <host-label|fqdn> <ipv4> [proxied:true|false]")
        sys.exit(2)

    name_in = sys.argv[1].strip()
    ip = sys.argv[2].strip()
    proxied = (sys.argv[3].strip().lower() == "true") if len(sys.argv) > 3 else False

    token = os.getenv("CF_API_TOKEN", "").strip()
    zone_id = os.getenv("CF_ZONE_ID", "").strip()
    zone_name = os.getenv("CF_ZONE_NAME", "jambetter.music").strip()

    if not token:
        die("CF_API_TOKEN is required")
    if not zone_id:
        die("CF_ZONE_ID is required")

    # normalize name
    if name_in.endswith("." + zone_name) or name_in == zone_name:
        fqdn = name_in
    else:
        fqdn = f"{name_in}.{zone_name}" if name_in != "@" else zone_name

    # list existing A record for fqdn
    query = f"/zones/{zone_id}/dns_records?type=A&name={urllib.parse.quote(fqdn)}"
    try:
        found = req("GET", query, token)
    except urllib.error.HTTPError as e:
        die(f"Cloudflare API GET failed: {e}")

    if not found.get("success"):
        die(f"Cloudflare API GET unsuccessful: {found}")

    existing = (found.get("result") or [])

    body = {
        "type": "A",
        "name": fqdn,
        "content": ip,
        "ttl": 1,  # auto
        "proxied": proxied,
    }

    try:
        if existing:
            rec_id = existing[0].get("id")
            out = req("PUT", f"/zones/{zone_id}/dns_records/{rec_id}", token, body)
            action = "updated"
        else:
            out = req("POST", f"/zones/{zone_id}/dns_records", token, body)
            action = "created"
    except urllib.error.HTTPError as e:
        die(f"Cloudflare API write failed: {e.read().decode(utf-8, ignore)}")

    if not out.get("success"):
        die(f"Cloudflare API write unsuccessful: {out}")

    result = out.get("result") or {}
    print(json.dumps({
        "ok": True,
        "action": action,
        "name": result.get("name", fqdn),
        "content": result.get("content", ip),
        "proxied": result.get("proxied", proxied),
        "id": result.get("id"),
    }, indent=2))


if __name__ == "__main__":
    main()
