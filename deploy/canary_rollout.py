#!/usr/bin/env python3
import json, subprocess, sys, time, urllib.request

if len(sys.argv) < 2:
    raise SystemExit("Usage: canary_rollout.py <git-tag> [inventory] [batch_size] [dry_run:0|1]")

tag = sys.argv[1]
inv_path = sys.argv[2] if len(sys.argv) > 2 else "/home/nds/deploy/fleet_inventory.json"
batch_size = int(sys.argv[3]) if len(sys.argv) > 3 else 5
dry_run = (sys.argv[4] == "1") if len(sys.argv) > 4 else False

inv = json.load(open(inv_path))
servers = inv.get("servers", [])
repo = inv.get("repo", "/home/nds")
services = inv.get("services", [])

if not servers:
    raise SystemExit("No servers in inventory")

canary = next((s for s in servers if s.get("canary")), servers[0])
others = [s for s in servers if s is not canary]


def run(cmd):
    print("  $", cmd)
    if dry_run:
        return 0
    return subprocess.run(cmd, shell=True).returncode


def deploy_one(s):
    host = s["ssh_host"]
    user = s.get("ssh_user", "ubuntu")
    svc_restart = " ".join(services)
    remote = f"cd {repo} && git fetch --tags origin && git checkout {tag} && sudo systemctl restart {svc_restart}"
    cmd = f"ssh -o BatchMode=yes -o ConnectTimeout=12 {user}@{host} {json.dumps(remote)}"
    return run(cmd) == 0


def check_health(s, retries=12, wait=5):
    url = s.get("health_url", "")
    token = s.get("ops_token", "")
    if not url:
        return False
    for _ in range(retries):
        try:
            req = urllib.request.Request(url)
            if token:
                req.add_header("X-Ops-Token", token)
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            q = (data.get("quality") or {}).get("grade")
            services_ok = all(x.get("active") for x in data.get("services", []))
            if q in ("green", "yellow") and services_ok:
                print(f"  health OK ({q})")
                return True
            print(f"  health not ready (grade={q}, services_ok={services_ok})")
        except Exception as e:
            print("  health check error:", e)
        time.sleep(wait)
    return False


def deploy_and_gate(s):
    print(f"\n=== Deploy {s.get('name', s.get('id'))} ===")
    if not deploy_one(s):
        print("Deploy failed")
        return False
    if dry_run:
        return True
    return check_health(s)

print("Canary server:", canary.get("name", canary.get("id")))
if not deploy_and_gate(canary):
    raise SystemExit("Canary failed. Stopping rollout.")

if not others:
    print("\nNo additional servers. Rollout complete.")
    raise SystemExit(0)

for i in range(0, len(others), batch_size):
    batch = others[i:i+batch_size]
    print(f"\n--- Batch {i//batch_size+1}: {len(batch)} server(s) ---")
    ok = True
    for s in batch:
        if not deploy_and_gate(s):
            ok = False
            break
    if not ok:
        raise SystemExit("Batch failed. Stopping rollout.")

print("\nRollout complete ✅")
