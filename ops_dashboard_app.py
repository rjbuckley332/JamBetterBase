#!/usr/bin/env python3
import json
import os
import subprocess
import urllib.request
import urllib.parse
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

HEALTH_JSON = "/tmp/jambetter_health.json"
HEALTH_SCRIPT = "/home/nds/healthcheck.py"
OPS_TOKEN = os.getenv("OPS_DASHBOARD_TOKEN", "")
SERVERS_FILE = "/home/nds/ops_servers.json"

HTML = """
<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>JamBetter Fleet Ops</title>
<style>
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0e1116;color:#e7ecf3;margin:0;padding:16px}
.card{background:#161b22;border:1px solid #2a3443;border-radius:10px;padding:12px;margin-bottom:10px}
.row{display:flex;gap:10px;flex-wrap:wrap}.badge{padding:4px 8px;border-radius:999px;font-weight:700}
.green{background:#103f2a;color:#a9f5c8}.yellow{background:#4a3b12;color:#ffe089}.red{background:#4a1f1f;color:#ffb1b1}.gray{background:#2d3440;color:#d0d7e2}
th,td{padding:8px;border-bottom:1px solid #2a3443;text-align:left}table{width:100%;border-collapse:collapse}
.small{opacity:.78;font-size:12px}
</style></head><body>
<h2>JamBetter Fleet Ops Dashboard</h2>
<div class='row' id='top'></div>
<div class='card'><table><thead><tr><th>Server</th><th>Quality</th><th>Load</th><th>Disk</th><th>Reachability</th></tr></thead><tbody id='fleet'></tbody></table></div>
<div class='small' id='stamp'></div>
<script>
function b(g,l){const c=g==='green'?'green':(g==='yellow'?'yellow':(g==='red'?'red':'gray'));return `<span class='badge ${c}'>${l}</span>`}
async function load(){
  const r=await fetch('/fleet');
  const d=await r.json();
  const s=d.summary||{};
  document.getElementById('top').innerHTML = `
    <div class='card'>Servers<br><b>${s.total||0}</b></div>
    <div class='card'>Reachable<br><b>${s.reachable||0}/${s.total||0}</b></div>
    <div class='card'>Alerts<br><b>${s.alerts||0}</b></div>
  `;
  const rows=(d.servers||[]).map(x=>{
    if(!x.ok){
      return `<tr><td>${x.name}</td><td>${b('gray','Unknown')}</td><td>-</td><td>-</td><td>❌ ${x.error||'unreachable'}</td></tr>`;
    }
    const q=x.data.quality||{}; const L=x.data.load||{}; const D=x.data.disk||{};
    const reach = x.ok ? '✅ ok' : '❌';
    return `<tr><td>${x.name}</td><td>${b(q.grade||'gray', q.label||'Unknown')}</td><td>${L.load1 ?? '-'} / ${L.cores ?? '-'}</td><td>${D.used_pct ?? '-'}%</td><td>${reach}</td></tr>`;
  }).join('') || '<tr><td colspan="5">No servers configured</td></tr>';
  document.getElementById('fleet').innerHTML = rows;
  document.getElementById('stamp').textContent = 'Updated: ' + (d.ts || new Date().toISOString());
}
load(); setInterval(load, 15000);
</script></body></html>
"""


def _authorized() -> bool:
    if not OPS_TOKEN:
        return True
    tok = request.args.get("token") or request.headers.get("X-Ops-Token") or ""
    return tok == OPS_TOKEN


def _run_healthcheck():
    subprocess.run(["/usr/bin/python3", HEALTH_SCRIPT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _read_health():
    try:
        with open(HEALTH_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _load_servers():
    try:
        with open(SERVERS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get('servers', [])
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _fqdn_label(url: str) -> str:
    try:
        u = urllib.parse.urlparse(url)
        host = (u.hostname or '').strip()
        if not host:
            return ''
        return host.split('.')[0]
    except Exception:
        return ''

def _poll_server(entry: dict):
    url = entry.get('url') or ''
    label = _fqdn_label(url)
    name = entry.get('name') or label or entry.get('id') or 'unknown'
    token = entry.get('token') or ''
    timeout = float(entry.get('timeout', 4.0))
    if not url:
      return {'name': name, 'ok': False, 'error': 'missing url'}
    try:
        req_url = url
        req = urllib.request.Request(req_url)
        if token:
            req.add_header('X-Ops-Token', token)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode('utf-8', errors='ignore')
        data = json.loads(body)
        return {'name': name, 'ok': True, 'data': data}
    except Exception as e:
        return {'name': name, 'ok': False, 'error': str(e)}


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


@app.route('/fleet')
def fleet():
    if not _authorized():
        return (jsonify({'ok': False, 'error': 'unauthorized'}), 401)
    servers = _load_servers()
    out = []
    alerts = 0
    reachable = 0
    for e in servers:
        row = _poll_server(e)
        out.append(row)
        if row.get('ok'):
            reachable += 1
            q = ((row.get('data') or {}).get('quality') or {}).get('grade')
            if q in ('yellow','red'):
                alerts += 1
        else:
            alerts += 1
    return jsonify({
        'ok': True,
        'ts': __import__('datetime').datetime.utcnow().isoformat() + 'Z',
        'summary': {'total': len(servers), 'reachable': reachable, 'alerts': alerts},
        'servers': out,
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5090)
