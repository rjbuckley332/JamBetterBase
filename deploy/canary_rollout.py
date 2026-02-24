#!/usr/bin/env python3
import json, os, subprocess, sys, time, urllib.request, urllib.parse

if len(sys.argv) < 2:
    raise SystemExit("Usage: canary_rollout.py <git-tag> [inventory] [batch_size] [dry_run:0|1]")

tag = sys.argv[1]
inv_path = sys.argv[2] if len(sys.argv) > 2 else "/home/nds/deploy/fleet_inventory.json"
batch_size = int(sys.argv[3]) if len(sys.argv) > 3 else 5
dry_run = (sys.argv[4] == "1") if len(sys.argv) > 4 else False
SSH_OPTS = os.environ.get("SSH_OPTS", "-o BatchMode=yes -o ConnectTimeout=12 -o StrictHostKeyChecking=accept-new")
CADDY_ACME_CA = os.environ.get("CADDY_ACME_CA", "https://acme-v02.api.letsencrypt.org/directory")
CADDY_EMAIL = os.environ.get("CADDY_EMAIL", "")

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


def run_local(args):
    if dry_run:
        print("  $", " ".join(args))
        return 0, "", ""
    p = subprocess.run(args, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def fqdn_for_server(s):
    if s.get("fqdn"):
        return s["fqdn"].strip()
    url = s.get("health_url", "").strip()
    if not url:
        return ""
    try:
        return (urllib.parse.urlparse(url).hostname or "").strip()
    except Exception:
        return ""


def ensure_s3_bucket(s):
    bucket = (s.get("s3_bucket") or "").strip()
    if not bucket:
        return True

    region = (s.get("s3_region") or os.environ.get("LIBRARY_AWS_REGION") or "us-east-2").strip()
    print(f"  ensure s3 bucket: {bucket} (region={region})")

    rc, _, _ = run_local(["aws", "s3api", "head-bucket", "--bucket", bucket])
    if rc == 0:
        print("  s3 bucket exists")
        return True

    if dry_run:
        print("  s3 bucket would be created")
        return True

    create_cmd = ["aws", "s3api", "create-bucket", "--bucket", bucket, "--region", region]
    if region != "us-east-1":
        create_cmd += ["--create-bucket-configuration", f"LocationConstraint={region}"]

    rc, out, err = run_local(create_cmd)
    if rc != 0:
        print("  s3 bucket create failed:", (err or out).strip())
        return False

    print("  s3 bucket created")
    return True


def build_env_overrides(s):
    out = dict(s.get("env_overrides", {})) if isinstance(s.get("env_overrides"), dict) else {}

    if s.get("trackbot_rclone_remote"):
        out["TRACKBOT_RCLONE_REMOTE"] = str(s["trackbot_rclone_remote"]).strip()
    else:
        bucket = (s.get("s3_bucket") or "").strip()
        prefix = (s.get("s3_prefix") or "vps/vps-0001").strip("/")
        if bucket:
            out["TRACKBOT_RCLONE_REMOTE"] = f"s3pd:{bucket}/{prefix}"

    return out


def build_remote_command(s):
    fqdn = fqdn_for_server(s)
    app_port = int(s.get("app_port", 5000))
    svc_restart = " ".join(services)
    parts = []

    if fqdn:
        email_line = f"    email {CADDY_EMAIL}\\n" if CADDY_EMAIL else ""
        caddy = f'''cat >/tmp/Caddyfile.autogen <<'EOF'\n{{\n    acme_ca {CADDY_ACME_CA}\n{email_line}}}\n\n{fqdn} {{\n    encode gzip\n    reverse_proxy 127.0.0.1:{app_port}\n    header {{\n        X-Content-Type-Options "nosniff"\n        X-Frame-Options "SAMEORIGIN"\n        Referrer-Policy "no-referrer-when-downgrade"\n    }}\n}}\nEOF\nsudo install -m 644 /tmp/Caddyfile.autogen /etc/caddy/Caddyfile\nsudo caddy validate --config /etc/caddy/Caddyfile\nsudo systemctl restart caddy'''
        parts.append(caddy)

    env_overrides = build_env_overrides(s)
    if env_overrides:
        lines = []
        for k, v in env_overrides.items():
            key = str(k)
            val = str(v).replace('"', '\\"')
            lines.append(f'''if grep -q '^{{key}}=' /home/nds/.env; then sudo sed -i "s|^{{key}}=.*|{{key}}=\\\"{{val}}\\\"|" /home/nds/.env; else echo "{{key}}=\\\"{{val}}\\\"" | sudo tee -a /home/nds/.env >/dev/null; fi'''.replace('{key}', key).replace('{val}', val))
        parts.append(" && ".join(lines))

    deploy = f"cd {repo} && git fetch --tags origin && git checkout {tag} && sudo systemctl restart {svc_restart}"
    parts.append(deploy)
    return " && ".join(parts)


def deploy_one(s):
    host = s["ssh_host"]
    user = s.get("ssh_user", "ubuntu")
    remote = build_remote_command(s)
    cmd = f"ssh {SSH_OPTS} {user}@{host} {json.dumps(remote)}"
    ok = (run(cmd) == 0)
    if not ok:
        print("  SSH/deploy failed. If key is passphrase-protected, start ssh-agent and run ssh-add before rollout.")
    return ok


def check_health(s, retries=18, wait=5):
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
    fqdn = fqdn_for_server(s)
    if fqdn:
        print(f"  caddy host target: {fqdn}")
        print(f"  acme_ca: {CADDY_ACME_CA}")
    env_overrides = build_env_overrides(s)
    if env_overrides:
        print("  env overrides:", ", ".join(sorted(env_overrides.keys())))

    if not ensure_s3_bucket(s):
      print("S3 bucket preflight failed")
      return False

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
