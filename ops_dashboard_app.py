#!/usr/bin/env python3
import json
import os
import subprocess
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)
HEALTH_JSON = "/tmp/jambetter_health.json"
HEALTH_SCRIPT = "/home/nds/healthcheck.py"
OPS_TOKEN = os.getenv("OPS_DASHBOARD_TOKEN", "")
HTML = """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>JamBetter Ops</title><style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0e1116;color:#e7ecf3;margin:0;padding:16px}.card{background:#161b22;border:1px solid #2a3443;border-radius:10px;padding:12px;margin-bottom:10px}.row{display:flex;gap:10px;flex-wrap:wrap}.badge{padding:4px 8px;border-radius:999px;font-weight:700}.green{background:#103f2a;color:#a9f5c8}.yellow{background:#4a3b12;color:#ffe089}.red{background:#4a1f1f;color:#ffb1b1}th,td{padding:8px;border-bottom:1px solid #2a3443;text-align:left}table{width:100%;border-collapse:collapse}</style></head><body><h2>JamBetter Ops Dashboard</h2><div class='row' id='top'></div><div class='card'><table><thead><tr><th>Service</th><th>State</th></tr></thead><tbody id='svc'></tbody></table></div><div id='stamp' style='opacity:.75'></div><script>function b(g,l){const c=g==='green'?'green':(g==='yellow'?'yellow':'red');return `<span class='badge ${c}'>${l}</span>`}async function load(){const r=await fetch('/health');const d=await r.json();const q=d.quality||{},L=d.load||{},D=d.disk||{};document.getElementById('top').innerHTML=`<div class='card'>Quality<br>${b(q.grade||'red',q.label||'Unknown')}</div><div class='card'>Load<br><b>${L.load1??'-'}</b> / ${L.cores??'-'} cores</div><div class='card'>Disk<br><b>${D.used_pct??'-'}%</b> used</div>`;const rows=(d.services||[]).map(s=>`<tr><td>${s.name}</td><td>${s.active?'✅ active':'❌ '+(s.state||'down')}</td></tr>`).join('')||'<tr><td colspan="2">No data</td></tr>';document.getElementById('svc').innerHTML=rows;document.getElementById('stamp').textContent='Updated: '+(d.ts||new Date().toISOString());}load();setInterval(load,15000);</script></body></html>"""

def _authorized():
    if not OPS_TOKEN: return True
    tok = request.args.get('token') or request.headers.get('X-Ops-Token') or ''
    return tok == OPS_TOKEN

def _run_healthcheck():
    subprocess.run(['/usr/bin/python3', HEALTH_SCRIPT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

def _read_health():
    try:
        with open(HEALTH_JSON,'r',encoding='utf-8') as f: return json.load(f)
    except Exception as e:
        return {'ok': False, 'error': str(e)}

@app.route('/')
def index():
    if not _authorized(): return ('Unauthorized',401)
    return render_template_string(HTML)

@app.route('/health')
def health():
    if not _authorized(): return (jsonify({'ok':False,'error':'unauthorized'}),401)
    _run_healthcheck(); return jsonify(_read_health())

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5090)
