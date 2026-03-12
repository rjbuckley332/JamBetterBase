#!/usr/bin/env python3
"""JamBetter unified ops dashboard (fleet view + lightweight pins).

This app is intended to run on an operator box and poll individual server-side
ops endpoints exposed by JamBetter instances.

Key endpoints:
- GET  /                      HTML dashboard
- GET  /api/servers            server inventory
- GET  /api/fleet              server status rollup (polls each server)
- POST /api/server/<id>/action start|stop|restart (proxy to server)
- GET/POST/DELETE /api/pins    lightweight operator notes + pinning

Auth:
- Optional shared token via OPS_DASHBOARD_TOKEN
  - supply as ?token=... or X-Ops-Token header
"""

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

HEALTH_JSON = os.getenv("JAMBETTER_HEALTH_JSON", "/tmp/jambetter_health.json")
HEALTH_SCRIPT = os.getenv("JAMBETTER_HEALTH_SCRIPT", "/home/nds/healthcheck.py")

OPS_TOKEN = os.getenv("OPS_DASHBOARD_TOKEN", "")

# Prefer repo-local servers file; fall back to legacy absolute path.
_DEFAULT_SERVERS_FILE = os.path.join(os.path.dirname(__file__), "ops_servers.json")
SERVERS_FILE = os.getenv(
    "OPS_SERVERS_FILE",
    _DEFAULT_SERVERS_FILE if os.path.exists(_DEFAULT_SERVERS_FILE) else "/home/nds/ops_servers.json",
)

PINS_FILE = os.getenv("OPS_PINS_FILE", "/tmp/jambetter_ops_pins.json")

HTML = """
<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>JamBetter Fleet Ops</title>
<style>
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0e1116;color:#e7ecf3;margin:0;padding:16px}
.card{background:#161b22;border:1px solid #2a3443;border-radius:10px;padding:12px;margin-bottom:10px}
.row{display:flex;gap:10px;flex-wrap:wrap}
.badge{padding:4px 8px;border-radius:999px;font-weight:700}
.green{background:#103f2a;color:#a9f5c8}
.yellow{background:#4a3b12;color:#ffe089}
.red{background:#4a1f1f;color:#ffb1b1}
.gray{background:#2d3440;color:#d0d7e2}
th,td{padding:8px;border-bottom:1px solid #2a3443;text-align:left}

table{width:100%;border-collapse:collapse}
.small{opacity:.78;font-size:12px}
.tabs{display:flex;gap:8px;margin:10px 0 12px 0}
.tab{cursor:pointer;padding:8px 10px;border-radius:10px;border:1px solid #2a3443;background:#0e1116;color:#e7ecf3}
.tab.active{background:#161b22}
.hidden{display:none}
.btn{cursor:pointer;border:1px solid #2a3443;background:#0e1116;color:#e7ecf3;border-radius:10px;padding:6px 10px}
.btn.danger{border-color:#6b2b2b}
input,textarea{width:100%;border:1px solid #2a3443;border-radius:10px;background:#0e1116;color:#e7ecf3;padding:8px}
textarea{min-height:90px}
</style></head><body>
<h2>JamBetter Unified Ops Dashboard</h2>

<div class='tabs'>
  <button class='tab active' data-tab='fleetTab'>Fleet</button>
  <button class='tab' data-tab='pinsTab'>Pins</button>
  <button class='tab' data-tab='healthTab'>Health</button>
</div>

<div id='fleetTab'>
  <div class='row' id='top'></div>
  <div class='card'>
    <table>
      <thead><tr><th>Server</th><th>Quality</th><th>Load</th><th>Disk</th><th>Reachability</th><th>Actions</th></tr></thead>
      <tbody id='fleet'></tbody>
    </table>
  </div>
</div>

<div id='pinsTab' class='hidden'>
  <div class='card'>
    <div style='display:flex; gap:10px; flex-wrap:wrap'>
      <div style='flex:1; min-width:260px'>
        <div class='small'>Title</div>
        <input id='pinTitle' placeholder='e.g., "vps-0001: restart loop"' />
      </div>
      <div style='flex:2; min-width:260px'>
        <div class='small'>Body</div>
        <textarea id='pinBody' placeholder='Notes, steps, links, context...'></textarea>
      </div>
      <div style='min-width:140px; align-self:flex-end'>
        <button class='btn' onclick='createPin()'>Create</button>
      </div>
    </div>
  </div>
  <div class='card'>
    <div class='small' style='margin-bottom:8px'>Pinned items (most recent first)</div>
    <div id='pins'></div>
  </div>
</div>

<div id='healthTab' class='hidden'>
  <div class='card'>
    <button class='btn' onclick='loadHealth()'>Refresh health</button>
    <pre id='healthOut' style='white-space:pre-wrap; margin-top:10px'></pre>
  </div>
</div>

<div class='small' id='stamp'></div>

<script>
function b(g,l){const c=g==='green'?'green':(g==='yellow'?'yellow':(g==='red'?'red':'gray'));return `<span class='badge ${c}'>${l}</span>`}

function setTab(tabId){
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active', t.dataset.tab===tabId));
  ['fleetTab','pinsTab','healthTab'].forEach(id=>document.getElementById(id).classList.toggle('hidden', id!==tabId));
}

document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click', ()=>setTab(t.dataset.tab)));

async function loadFleet(){
  const r=await fetch('/api/fleet');
  const d=await r.json();
  const s=d.summary||{};
  document.getElementById('top').innerHTML = `
    <div class='card'>Servers<br><b>${s.total||0}</b></div>
    <div class='card'>Reachable<br><b>${s.reachable||0}/${s.total||0}</b></div>
    <div class='card'>Alerts<br><b>${s.alerts||0}</b></div>
  `;

  const rows=(d.servers||[]).map(x=>{
    const name = x.name || x.id || 'unknown';
    const sid = x.id || '';
    if(!x.ok){
      return `<tr><td>${name}</td><td>${b('gray','Unknown')}</td><td>-</td><td>-</td><td>❌ ${x.error||'unreachable'}</td><td>${sid?`<button class='btn danger' onclick='serverAction("${sid}","restart")'>Restart</button>`:''}</td></tr>`;
    }
    const q=(x.data.quality||{});
    const L=(x.data.load||{});
    const D=(x.data.disk||{});
    const reach = x.ok ? '✅ ok' : '❌';
    return `<tr><td>${name}</td><td>${b(q.grade||'gray', q.label||'Unknown')}</td><td>${L.load1 ?? '-'} / ${L.cores ?? '-'}</td><td>${D.used_pct ?? '-'}%</td><td>${reach}</td><td>${sid?`<button class='btn danger' onclick='serverAction("${sid}","restart")'>Restart</button>`:''}</td></tr>`;
  }).join('') || '<tr><td colspan="6">No servers configured</td></tr>';

  document.getElementById('fleet').innerHTML = rows;
  document.getElementById('stamp').textContent = 'Updated: ' + (d.ts || new Date().toISOString());
}

async function serverAction(serverId, action){
  if(!serverId) return;
  if(!confirm(`Send ${action} to ${serverId}?`)) return;
  const r = await fetch(`/api/server/${encodeURIComponent(serverId)}/action`, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action})
  });
  const d = await r.json().catch(()=>({ok:false,error:'bad json'}));
  if(!r.ok || !d.ok){
    alert(`Action failed: ${d.error||r.status}`);
  } else {
    alert('Action sent.');
  }
  await loadFleet();
}

async function loadPins(){
  const r=await fetch('/api/pins');
  const d=await r.json();
  const items = (d.pins||[]);
  const html = items.map(p=>{
    const title = (p.title||'').replaceAll('<','&lt;');
    const body = (p.body||'').replaceAll('<','&lt;');
    return `
      <div class='card'>
        <div style='display:flex; justify-content:space-between; gap:10px; align-items:center'>
          <div><b>${p.pinned? '📌 ' : ''}${title}</b><div class='small'>${p.created_at||''}</div></div>
          <div style='display:flex; gap:8px'>
            <button class='btn' onclick='pinToggle("${p.id}", ${p.pinned? 'false':'true'})'>${p.pinned? 'Unpin':'Pin'}</button>
            <button class='btn danger' onclick='deletePin("${p.id}")'>Delete</button>
          </div>
        </div>
        <pre style='white-space:pre-wrap; margin-top:10px'>${body}</pre>
      </div>
    `;
  }).join('') || `<div class='small'>No pins yet.</div>`;
  document.getElementById('pins').innerHTML = html;
}

async function createPin(){
  const title = document.getElementById('pinTitle').value.trim();
  const body = document.getElementById('pinBody').value.trim();
  if(!title && !body){ alert('Add a title or body'); return; }
  const r = await fetch('/api/pins', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({title, body})});
  const d = await r.json().catch(()=>({ok:false}));
  if(!r.ok || !d.ok){ alert('Create failed: '+(d.error||r.status)); return; }
  document.getElementById('pinTitle').value='';
  document.getElementById('pinBody').value='';
  await loadPins();
}

async function pinToggle(id, pinned){
  const r = await fetch(`/api/pins/${encodeURIComponent(id)}/pin`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({pinned})});
  const d = await r.json().catch(()=>({ok:false}));
  if(!r.ok || !d.ok){ alert('Pin failed: '+(d.error||r.status)); return; }
  await loadPins();
}

async function deletePin(id){
  if(!confirm('Delete pin?')) return;
  const r = await fetch(`/api/pins/${encodeURIComponent(id)}`, {method:'DELETE'});
  const d = await r.json().catch(()=>({ok:false}));
  if(!r.ok || !d.ok){ alert('Delete failed: '+(d.error||r.status)); return; }
  await loadPins();
}

async function loadHealth(){
  const r=await fetch('/health');
  const d=await r.json();
  document.getElementById('healthOut').textContent = JSON.stringify(d, null, 2);
}

loadFleet();
loadPins();
setInterval(loadFleet, 15000);
</script></body></html>
"""


def _authorized() -> bool:
    if not OPS_TOKEN:
        return True
    tok = request.args.get("token") or request.headers.get("X-Ops-Token") or ""
    return tok == OPS_TOKEN


def _utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_healthcheck() -> None:
    try:
        subprocess.run(
            ["/usr/bin/python3", HEALTH_SCRIPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass


def _read_health() -> dict:
    try:
        with open(HEALTH_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"ok": False, "error": str(e), "path": HEALTH_JSON}


def _load_servers() -> list[dict[str, Any]]:
    try:
        with open(SERVERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("servers", [])
        if isinstance(data, list):
            # normalize basic fields
            out = []
            for i, s in enumerate(data):
                if not isinstance(s, dict):
                    continue
                sid = (s.get("id") or s.get("name") or f"srv-{i+1}").strip()
                out.append({
                    "id": sid,
                    "name": (s.get("name") or sid).strip(),
                    "url": (s.get("url") or "").strip(),
                    "token": (s.get("token") or "").strip(),
                    "timeout": s.get("timeout", 4.0),
                })
            return out
    except Exception:
        pass
    return []


def _server_by_id(server_id: str) -> dict | None:
    server_id = (server_id or "").strip()
    if not server_id:
        return None
    for s in _load_servers():
        if s.get("id") == server_id:
            return s
    return None


def _fqdn_label(url: str) -> str:
    try:
        u = urllib.parse.urlparse(url)
        host = (u.hostname or "").strip()
        if not host:
            return ""
        return host.split(".")[0]
    except Exception:
        return ""


def _http_json(url: str, *, method: str = "GET", headers: dict | None = None, payload: dict | None = None, timeout: float = 4.0) -> tuple[int, dict]:
    data = None
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="ignore")
            return r.getcode(), (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="ignore")
            return e.code, (json.loads(body) if body else {"error": body})
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}


def _poll_server(entry: dict) -> dict:
    url = (entry.get("url") or "").strip()
    label = _fqdn_label(url)
    name = entry.get("name") or label or entry.get("id") or "unknown"
    sid = entry.get("id") or name
    token = (entry.get("token") or "").strip()
    timeout = float(entry.get("timeout", 4.0))

    if not url:
        return {"id": sid, "name": name, "ok": False, "error": "missing url"}

    # Prefer a stable server-side status API.
    candidates = [
        url.rstrip("/") + "/api/support/status",
        url.rstrip("/") + "/ops/status",
        url.rstrip("/") + "/status",
        url.rstrip("/") + "/",
    ]

    headers = {}
    if token:
        headers["X-Ops-Token"] = token

    last_err = None
    for u in candidates:
        code, data = _http_json(u, headers=headers, timeout=timeout)
        if code and 200 <= code < 300 and isinstance(data, dict):
            return {"id": sid, "name": name, "ok": True, "data": data, "source": u}
        last_err = data.get("error") if isinstance(data, dict) else str(data)

    return {"id": sid, "name": name, "ok": False, "error": last_err or "unreachable"}


# --- pins (D1-style read/pin API, file-backed for now) ---

def _pins_read() -> dict:
    try:
        with open(PINS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if not isinstance(data, dict):
            return {"pins": []}
        pins = data.get("pins") or []
        if not isinstance(pins, list):
            pins = []
        return {"pins": pins}
    except FileNotFoundError:
        return {"pins": []}
    except Exception:
        return {"pins": []}


def _pins_write(pins: list[dict]) -> None:
    tmp = PINS_FILE + ".tmp"
    os.makedirs(os.path.dirname(PINS_FILE) or "/tmp", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"pins": pins}, f)
    os.replace(tmp, PINS_FILE)


def _pin_sort_key(p: dict) -> tuple:
    # pinned first, newest first
    pinned = bool(p.get("pinned"))
    created = p.get("created_at") or ""
    return (0 if pinned else 1, "" if not created else "".join(reversed(created)))


@app.route("/")
def index():
    if not _authorized():
        return ("Unauthorized", 401)
    return render_template_string(HTML)


@app.route("/health")
def health():
    if not _authorized():
        return (jsonify({"ok": False, "error": "unauthorized"}), 401)
    _run_healthcheck()
    return jsonify(_read_health())


@app.route("/api/servers")
def api_servers():
    if not _authorized():
        return (jsonify({"ok": False, "error": "unauthorized"}), 401)
    servers = _load_servers()
    return jsonify({
        "ok": True,
        "ts": _utc_ts(),
        "servers": [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "url": s.get("url"),
            }
            for s in servers
        ],
    })


@app.route("/api/fleet")
def api_fleet():
    if not _authorized():
        return (jsonify({"ok": False, "error": "unauthorized"}), 401)

    servers = _load_servers()
    out = []
    alerts = 0
    reachable = 0

    for e in servers:
        row = _poll_server(e)
        out.append(row)
        if row.get("ok"):
            reachable += 1
            q = ((row.get("data") or {}).get("quality") or {}).get("grade")
            if q in ("yellow", "red"):
                alerts += 1
        else:
            alerts += 1

    return jsonify({
        "ok": True,
        "ts": _utc_ts(),
        "summary": {"total": len(servers), "reachable": reachable, "alerts": alerts},
        "servers": out,
    })


# Back-compat alias
@app.route("/fleet")
def fleet_alias():
    return api_fleet()


@app.route("/api/server/<server_id>/action", methods=["POST"])
def api_server_action(server_id: str):
    if not _authorized():
        return (jsonify({"ok": False, "error": "unauthorized"}), 401)

    server = _server_by_id(server_id)
    if not server:
        return (jsonify({"ok": False, "error": "unknown server"}), 404)

    d = request.get_json(silent=True) or {}
    action = (d.get("action") or "").strip().lower()
    if action not in ("start", "stop", "restart"):
        return (jsonify({"ok": False, "error": "invalid action"}), 400)

    base = (server.get("url") or "").rstrip("/")
    if not base:
        return (jsonify({"ok": False, "error": "missing server url"}), 400)

    # Convention: server control endpoints.
    # (Each JamBetter server should implement these. If not present, this will 404.)
    endpoint = f"{base}/api/jamulus/{action}"

    headers = {}
    if server.get("token"):
        headers["X-Ops-Token"] = server["token"]

    code, data = _http_json(endpoint, method="POST", headers=headers, payload={}, timeout=12.0)
    if code and 200 <= code < 300 and isinstance(data, dict) and data.get("ok") is not False:
        return jsonify({"ok": True, "server": server_id, "action": action, "upstream": data})

    return (
        jsonify({"ok": False, "error": "upstream failed", "upstream_code": code, "upstream": data}),
        502,
    )


@app.route("/api/pins", methods=["GET", "POST"])
def api_pins():
    if not _authorized():
        return (jsonify({"ok": False, "error": "unauthorized"}), 401)

    if request.method == "GET":
        pins = _pins_read().get("pins") or []
        # pinned first, newest first
        def sort_key(p: dict):
            pinned = bool(p.get("pinned"))
            created = p.get("created_at") or ""
            return (0 if pinned else 1, created)

        pins_sorted = sorted(pins, key=sort_key, reverse=True)
        return jsonify({"ok": True, "ts": _utc_ts(), "pins": pins_sorted})

    d = request.get_json(silent=True) or {}
    title = (d.get("title") or "").strip()
    body = (d.get("body") or "").strip()
    if not title and not body:
        return (jsonify({"ok": False, "error": "missing title/body"}), 400)

    pins = _pins_read().get("pins") or []
    pid = os.urandom(8).hex()
    pins.append({
        "id": pid,
        "title": title,
        "body": body,
        "pinned": True,
        "created_at": _utc_ts(),
    })
    _pins_write(pins)
    return jsonify({"ok": True, "id": pid})


@app.route("/api/pins/<pin_id>/pin", methods=["POST"])
def api_pins_pin(pin_id: str):
    if not _authorized():
        return (jsonify({"ok": False, "error": "unauthorized"}), 401)

    d = request.get_json(silent=True) or {}
    pinned = bool(d.get("pinned"))

    pins = _pins_read().get("pins") or []
    for p in pins:
        if p.get("id") == pin_id:
            p["pinned"] = pinned
            _pins_write(pins)
            return jsonify({"ok": True})
    return (jsonify({"ok": False, "error": "not found"}), 404)


@app.route("/api/pins/<pin_id>", methods=["DELETE"])
def api_pins_delete(pin_id: str):
    if not _authorized():
        return (jsonify({"ok": False, "error": "unauthorized"}), 401)

    pins = _pins_read().get("pins") or []
    kept = [p for p in pins if p.get("id") != pin_id]
    if len(kept) == len(pins):
        return (jsonify({"ok": False, "error": "not found"}), 404)
    _pins_write(kept)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("OPS_DASHBOARD_PORT", "5090")))
