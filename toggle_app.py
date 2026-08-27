from flask import Flask, request, jsonify, render_template, make_response, session, redirect, send_file
import os, subprocess, json, glob, socket, threading, time, math, struct, tempfile, zipfile, shutil, re, hmac, hashlib
import fcntl
import boto3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import urllib.parse
import urllib.request
import uuid as uuidlib

# ---------- TIMEZONE ----------
# We want human-facing timestamps (filenames/logs) in Eastern time.
# This handles EST/EDT automatically.
try:
    from zoneinfo import ZoneInfo  # py3.9+
    _LOCAL_TZ = ZoneInfo("America/New_York")
except Exception:
    _LOCAL_TZ = None

def _now_local() -> datetime:
    if _LOCAL_TZ is None:
        return datetime.now()
    return datetime.now(_LOCAL_TZ)

def _recording_map_key_now() -> str:
    """Local timezone key that matches tenant-local date boundaries."""
    return _now_local().strftime('%Y%m%d_%H%M%S')

app = Flask(__name__)

# Secret for signed session cookies (set env WEB_SESSION_SECRET to persist across restarts)
app.secret_key = os.getenv("WEB_SESSION_SECRET") or "b0d4488ec7feed6053da40a816518a8026518171275576c5ac64d41ea424de28"
# ---------- Auto-restart on silence state ----------
_auto_restart_state = {"enabled": False, "silence_seconds": 6}
_support_sessions: dict[str, dict] = {}
_support_faq_cache: dict[str, object] = {'mtime': None, 'data': None}
_support_first_recording_cache: dict[str, object] = {'checked_at': 0.0, 'value': None}

app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# ---------- WEB SECURITY (passcode) ----------
# Passcode gate disabled (we may reintroduce auth at the reverse-proxy layer later).
WEB_PASSCODE = ""

# Ops token for machine-to-machine control (fleet dashboard polling + start/stop).
# If set, callers must supply X-Ops-Token: <token> (or ?token=... for GETs).
OPS_SERVER_TOKEN = (os.getenv("OPS_SERVER_TOKEN", "") or "").strip()


# ---------- CONSTANTS / PATHS ----------
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
RECORDING_FLAG  = "/tmp/jamulus_recording.flag"  # legacy UI flag (not authoritative)
LOCK_FILE       = "/tmp/jamulus_locked.flag"
SESSION_STATUS  = "/tmp/jamulus_session_active.flag"  # legacy UI flag (not authoritative)
INCLUDE_INJECTOR_FLAG = "/tmp/jamulus_include_injector.flag"
INCLUDE_INJECTOR_MAP_CSV = "/tmp/jamulus_include_injector_map.csv"
METRONOME_TAINT_MAP_CSV = "/tmp/jamulus_metronome_taint_map.csv"
ACTIVE_RECORDING_KEY_FILE = "/tmp/jamulus_active_recording_key.txt"

# Jamulus recordings parent dir
RECORDINGS_DIR  = "/var/lib/jamulus/recordings"

# Optional: Jamulus JSON-RPC (preferred; explicit start/stop + true recorder status)
JSONRPC_HOST = os.getenv("JAMULUS_JSONRPC_HOST", "127.0.0.1")
JSONRPC_PORT = int(os.getenv("JAMULUS_JSONRPC_PORT", "22100"))
JSONRPC_SECRET_FILE = os.getenv("JAMULUS_JSONRPC_SECRET_FILE", "/var/lib/jamulus/jsonrpc-secret.txt")
TENANT_JSONRPC_PORTS = {
    'pd': int(os.getenv('JAMULUS_JSONRPC_PORT_PD', '23100')),
    'vc': int(os.getenv('JAMULUS_JSONRPC_PORT_VC', '23101')),
    'seigr': int(os.getenv('JAMULUS_JSONRPC_PORT_SEIGR', '23102')),
}
TENANT_RECORDING_DIRS = {
    'pd': (os.getenv('JAMULUS_RECORDINGS_DIR_PD', '') or '').strip(),
    'vc': (os.getenv('JAMULUS_RECORDINGS_DIR_VC', '') or '').strip(),
    'seigr': (os.getenv('JAMULUS_RECORDINGS_DIR_SEIGR', '') or '').strip(),
}

SAVED_NAMES_CSV = os.path.join(BASE_DIR, "saved_session_names.csv")
SESSION_LOG_JSN = os.path.join(BASE_DIR, "session_name_log.json")
RECORDING_MAP_CSV = "/home/nds/recording_name_map.csv"

HOURS_BLOCK     = 10
# --------------------------------------


# ---------- SOLO MODE (auto restart learning track on singer silence) ----------
SOLO_MODE = False
SOLO_SILENCE_SECONDS = 6.0
SOLO_THRESHOLD_DBFS = -50.0  # conservative
SOLO_POLL_SECONDS = 1.0
_solo_thread = None
_solo_stop = threading.Event()
_solo_state = {
    'monitor_file': None,
    'silence_started': None,
    'last_restart': 0.0,
}





# ---------- LIBRARY (S3) ----------
LIBRARY_S3_BUCKET = os.getenv('LIBRARY_S3_BUCKET', 'pipedreamers-recordings-prod')
LIBRARY_VPS_ID    = os.getenv('LIBRARY_VPS_ID', 'vps-0001')
LIBRARY_AWS_CLI   = os.getenv('LIBRARY_AWS_CLI', '/home/nds/.local/bin/aws')
LIBRARY_AWS_REGION = os.getenv('LIBRARY_AWS_REGION', 'us-east-1')
UPLOAD_SAVED_ROOT = 'saved'
UPLOAD_FOLDER_MARKER = '.keep'
# Keys live under: vps/<vps-id>/recordings/ and vps/<vps-id>/tracks/

# ---------- TRACKBOT (Injector WAV Playback) ----------
TRACKBOT_BASE_URL = os.getenv("TRACKBOT_BASE_URL", "http://127.0.0.1:8088").rstrip("/")
TRACKBOT_QUEUE_FILE = os.getenv("TRACKBOT_QUEUE_FILE", "/tmp/trackbot_queue.json")

# Per-tenant TrackBot routing (port + queue file)
TENANT_TRACKBOT_PORTS = {
    'pd':    int(os.getenv('TRACKBOT_PORT_PD',    '8088')),
    'vc':    int(os.getenv('TRACKBOT_PORT_VC',    '8089')),
    'seigr': int(os.getenv('TRACKBOT_PORT_SEIGR', '8090')),
}

def _trackbot_url(tenant=None):
    """Return the TrackBot base URL for the given tenant."""
    if tenant and tenant in TENANT_TRACKBOT_PORTS:
        return f"http://127.0.0.1:{TENANT_TRACKBOT_PORTS[tenant]}"
    return TRACKBOT_BASE_URL

def _queue_file(tenant=None):
    """Return the queue file path for the given tenant."""
    if tenant and tenant != 'pd':
        return f"/tmp/trackbot_queue_{tenant}.json"
    return TRACKBOT_QUEUE_FILE
SUPPORT_BOT_URL = os.getenv("SUPPORT_BOT_URL", "https://t.me/JamBetterBot").strip()
SUPPORT_WELCOME_DAYS = int(os.getenv("SUPPORT_WELCOME_DAYS", "6"))
SUPPORT_WELCOME_PHONE = (os.getenv("SUPPORT_WELCOME_PHONE", "") or "").strip()
SUPPORT_WELCOME_PIN = (os.getenv("SUPPORT_WELCOME_PIN", "") or "").strip()
SUPPORT_EMAIL_TO = (os.getenv("SUPPORT_EMAIL_TO", "") or "").strip()
SUPPORT_EMAIL_BCC = (os.getenv("SUPPORT_EMAIL_BCC", "rbuckley@reachhigher.ai") or "").strip()
SUPPORT_EMAIL_FROM = (os.getenv("SUPPORT_EMAIL_FROM", "JamBetter Support <support@jambetter.music>") or "").strip()
RESEND_API_KEY = (os.getenv("RESEND_API_KEY", "") or "").strip()


def create_app():
    """Factory for WSGI servers (gunicorn)."""
    return app

@app.route('/login', methods=['GET','POST'])
def login():
    # If no passcode set, consider it open (but you really should set WEB_PASSCODE)
    if not WEB_PASSCODE:
        session['authed'] = True
        return redirect('/')

    if request.method == 'POST':
        code = (request.form.get('passcode') or '').strip()
        if code == WEB_PASSCODE:
            session['authed'] = True
            return redirect('/')
        return ('Invalid code', 403)

    return ("""
<!doctype html>
<html><head><meta name=viewport content='width=device-width, initial-scale=1'>
<title>PipeDreamers Control Login</title>
<style>body{font-family:system-ui,Segoe UI,Arial;margin:24px;}input{font-size:18px;padding:10px;width:260px;}button{font-size:18px;padding:10px 14px;margin-left:8px;}</style>
</head>
<body>
<h2>PipeDreamers Jamulus Control</h2>
<form method='POST'>
  <label>Access code:</label><br>
  <input name='passcode' type='password' autofocus />
  <button type='submit'>Enter</button>
</form>
</body></html>
""", 200, {'Cache-Control':'no-store'})

@app.route('/logout', methods=['POST','GET'])
def logout():
    session.clear()
    return redirect('/login')

@app.route('/')
def index():
    # Gate the page itself.
    if WEB_PASSCODE and not session.get('authed'):
        return redirect('/login')
    resp = make_response(render_template('updated_toggle_app_script.html', support_bot_url=SUPPORT_BOT_URL, support_server_id=LIBRARY_VPS_ID))
    resp.headers['Cache-Control'] = 'no-store'
    return resp

def _require_passcode():
    """Passcode gate disabled: always allow."""
    return True, None


def _require_ops_token():
    """Optional shared token for ops surfaces (fleet dashboard + automation).

    If OPS_SERVER_TOKEN is set, require a matching token in:
      - X-Ops-Token header, or
      - ?token=... query param (GET convenience).

    Note: This is intentionally separate from WEB_PASSCODE.
    """
    if not OPS_SERVER_TOKEN:
        return True, None
    tok = (request.headers.get('X-Ops-Token') or request.args.get('token') or '').strip()
    if tok != OPS_SERVER_TOKEN:
        return False, (jsonify({'ok': False, 'error': 'unauthorized'}), 401)
    return True, None


    # If logged-in via cookie, allow (covers page + API calls from that page)
    if session.get('authed'):
        return True, None

    supplied = request.headers.get('X-Passcode')
    if not supplied:
        # Try JSON body (for POSTs)
        try:
            j = request.get_json(silent=True) or {}
            supplied = j.get('passcode')
        except Exception:
            supplied = None
    if not supplied:
        supplied = request.args.get('passcode')

    if supplied != WEB_PASSCODE:
        return False, ("Unauthorized", 403)
    return True, None


def _queue_read(tenant=None) -> dict:
    qf = _queue_file(tenant)
    try:
        with open(qf, "r") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def _queue_write(d: dict, tenant=None):
    qf = _queue_file(tenant)
    tmp = qf + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, qf)

def _queue_clear(tenant=None):
    qf = _queue_file(tenant)
    try:
        os.remove(qf)
    except FileNotFoundError:
        pass

def _http_post_json(url: str, payload: dict | None = None, timeout: float = 2.0) -> tuple[int, str]:
    # stdlib only (avoid new deps)
    import urllib.request
    import urllib.error
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='replace')
    except Exception as e:
        return 0, str(e)

def _http_get(url: str, timeout: float = 2.0) -> tuple[int, str]:
    import urllib.request
    import urllib.error
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='replace')
    except Exception as e:
        return 0, str(e)



# ---------- S3 Library helpers ----------
def _aws(args: list[str], timeout: float = 10.0) -> tuple[int, str]:
    # Run AWS CLI and return (rc, combined_output).
    cmd = [LIBRARY_AWS_CLI, *args, "--region", LIBRARY_AWS_REGION]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    return p.returncode, p.stdout


def _lib_prefix(area: str, subprefix: str = "") -> str:
    area = (area or "").strip().lower()
    if area not in ("recordings", "tracks"):
        area = "recordings"
    base = f"vps/{LIBRARY_VPS_ID}/{area}/"
    sub = (subprefix or "").lstrip("/")
    sub = sub.replace("..", "")
    return base + sub


def _tenant_slug_from_request() -> str | None:
    host = (request.headers.get('X-Forwarded-Host') or request.host or '').split(':', 1)[0].strip().lower()
    if host.endswith('.jambetter.music'):
        sub = host.split('.', 1)[0]
        if sub in ('pd', 'vc', 'seigr'):
            return sub
    forced = (os.getenv('LIBRARY_TENANT') or '').strip().lower()
    return forced or None


def _apply_tenant_scope(area: str, sub: str, *, for_listing: bool = False) -> tuple[str, str] | tuple[None, None]:
    """Apply host-based tenant scoping to library paths.

    recordings on tenant hosts are constrained to recordings/<tenant>/..., with one
    deliberate shared exception: recordings/library/ remains visible so all tenants
    can access the shared song library.

    - listing recordings/ on a tenant host is rewritten to recordings/<tenant>/
    - paths already rooted at recordings/<tenant>/ are allowed
    - bare tenant-relative paths like recordings/2026-03-27/ are rewritten to recordings/<tenant>/2026-03-27/
    - shared utility path recordings/library/ is allowed unchanged
    - explicit paths for a different tenant are rejected
    tracks remain shared unless explicitly scoped elsewhere
    """
    tenant = _tenant_slug_from_request()
    sub = (sub or '').strip('/').replace('\\', '/')
    if area == 'recordings' and tenant:
        shared_roots = {'library'}
        hidden_roots = {'trash', '.trash', '.archived'}
        if not sub:
            sub = tenant
        elif sub == tenant or sub.startswith(tenant + '/'):
            pass
        else:
            first = sub.split('/', 1)[0].lower()
            known_tenants = {'pd', 'vc', 'seigr'}
            if first in shared_roots:
                pass
            elif first in hidden_roots:
                return None, None
            elif first in known_tenants:
                return None, None
            else:
                sub = f'{tenant}/{sub}'
    if sub:
        sub += '/'
    return area, sub


def _safe_library_path_subpath(path_value: str) -> tuple[str, str] | tuple[None, None]:
    """Return (area, subpath) for a user path like recordings/foo/bar/.

    area in {recordings, tracks}; subpath is normalized relative path with trailing slash allowed.
    Applies host-based tenant scoping for recordings.
    """
    raw = (path_value or '').strip().lstrip('/')
    if not raw:
        return None, None
    raw = raw.replace('\\', '/')
    if '..' in raw:
        return None, None
    parts = [p for p in raw.split('/') if p]
    if not parts:
        return None, None
    area = parts[0].lower()
    if area not in ('recordings', 'tracks'):
        return None, None
    sub = '/'.join(parts[1:])
    return _apply_tenant_scope(area, sub)


def _safe_library_file_path(path_value: str) -> tuple[str, str] | tuple[None, None]:
    """Return (area, relative_file_path) for a user file path like recordings/foo/bar.wav.

    Applies host-based tenant scoping for recordings.
    """
    raw = (path_value or '').strip().lstrip('/')
    if not raw:
        return None, None
    raw = raw.replace('\\', '/')
    if '..' in raw:
        return None, None
    parts = [p for p in raw.split('/') if p]
    if len(parts) < 2:
        return None, None
    area = parts[0].lower()
    if area not in ('recordings', 'tracks'):
        return None, None
    sub = '/'.join(parts[1:-1])
    area, scoped_sub = _apply_tenant_scope(area, sub)
    if not area:
        return None, None
    filename = parts[-1]
    rel = ((scoped_sub or '') + filename).lstrip('/')
    return area, rel


def _sanitize_folder_name(raw_name: str) -> str:
    name = re.sub(r'\s+', ' ', str(raw_name or '').strip())
    name = name.replace('/', ' ').replace('\\', ' ')
    name = re.sub(r'[^A-Za-z0-9 _\-().&]', '', name)
    name = re.sub(r'\s+', ' ', name).strip(' .')
    if len(name) > 80:
        name = name[:80].rstrip(' .')
    return name


def _saved_folder_key(tenant: str, folder_name: str) -> str:
    safe_folder = _sanitize_folder_name(folder_name)
    return f"vps/{LIBRARY_VPS_ID}/recordings/{tenant}/{UPLOAD_SAVED_ROOT}/{safe_folder}/{UPLOAD_FOLDER_MARKER}"


def _ensure_saved_folder(tenant: str, folder_name: str) -> str:
    safe_folder = _sanitize_folder_name(folder_name)
    if not safe_folder:
        raise ValueError('folder name is required')
    key = _saved_folder_key(tenant, safe_folder)
    s3 = boto3.client('s3', region_name=LIBRARY_AWS_REGION)
    s3.put_object(Bucket=LIBRARY_S3_BUCKET, Key=key, Body=b'')
    return safe_folder


def _zip_filename_for_subpath(subpath: str, fallback: str = 'session') -> str:
    bits = [b for b in (subpath or '').split('/') if b]
    base = bits[-1] if bits else fallback
    safe = ''.join(ch if ch.isalnum() or ch in ('-','_') else '_' for ch in base)
    return f"{safe or fallback}.zip"


def _archived_marker_prefix(area: str) -> str:
    # Where we store archived markers in S3 (per area)
    return f"vps/{LIBRARY_VPS_ID}/{area}/.archived/"


def _archived_marker_key(area: str, dir_name: str) -> str:
    # marker per immediate child directory (store safe filename)
    safe = urllib.parse.quote((dir_name or '').strip(), safe='')
    return _archived_marker_prefix(area) + safe + '.marker'


def _list_archived_dirs(area: str) -> set[str]:
    # Return set of quoted dir names that are archived at the area root.
    try:
        s3 = boto3.client('s3', region_name=LIBRARY_AWS_REGION)
        pref = _archived_marker_prefix(area)
        out = set()
        token = None
        while True:
            kw = {'Bucket': LIBRARY_S3_BUCKET, 'Prefix': pref}
            if token:
                kw['ContinuationToken'] = token
            resp = s3.list_objects_v2(**kw)
            for obj in resp.get('Contents') or []:
                key = obj.get('Key') or ''
                if not key.startswith(pref):
                    continue
                name = key[len(pref):]
                if name.endswith('.marker'):
                    out.add(name[:-7])
            if resp.get('IsTruncated'):
                token = resp.get('NextContinuationToken')
            else:
                break
        return out
    except Exception:
        return set()
def _s3_list(prefix: str) -> dict:
    # List S3 objects under a prefix.
    import json
    rc1, out1 = _aws([
        "s3api", "list-objects-v2",
        "--bucket", LIBRARY_S3_BUCKET,
        "--prefix", prefix,
        "--delimiter", "/",
    ], timeout=15.0)
    if rc1 != 0:
        return {"ok": False, "error": out1.strip(), "prefix": prefix}

    data = json.loads(out1 or "{}")
    dirs = [cp.get("Prefix") for cp in (data.get("CommonPrefixes") or []) if cp.get("Prefix")]

    files = []
    for obj in (data.get("Contents") or []):
        key = obj.get("Key")
        if (not key) or key.endswith("/") or key == prefix:
            continue
        if key.split('/')[-1] == UPLOAD_FOLDER_MARKER:
            continue
        files.append({
            "key": key,
            "name": key.split("/")[-1],
            "size": obj.get("Size", 0),
            "lastModified": obj.get("LastModified"),
        })

    return {
        "ok": True,
        "prefix": prefix,
        "dirs": [d[len(prefix):].rstrip("/") for d in dirs if d.startswith(prefix)],
        "files": files,
    }


def _s3_presign(key: str, expires: int = 900, attachment: bool = True) -> dict:
    key = (key or "").lstrip("/")
    allowed1 = f"vps/{LIBRARY_VPS_ID}/recordings/"
    allowed2 = f"vps/{LIBRARY_VPS_ID}/tracks/"
    if not (key.startswith(allowed1) or key.startswith(allowed2)):
        return {"ok": False, "error": "key not allowed"}

    try:
        s3 = boto3.client('s3', region_name=LIBRARY_AWS_REGION)
        filename = key.split('/')[-1]
        params = {
            'Bucket': LIBRARY_S3_BUCKET,
            'Key': key,
        }
        if attachment:
            params['ResponseContentDisposition'] = f'attachment; filename="{filename}"'
        else:
            params['ResponseContentDisposition'] = f'inline; filename="{filename}"'
        url = s3.generate_presigned_url(
            ClientMethod='get_object',
            Params=params,
            ExpiresIn=int(expires),
        )
        return {"ok": True, "url": url, "key": key}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _is_shared_library(area: str | None, rel: str | None) -> bool:
    return area == 'recordings' and str(rel or '').startswith('library/')


def _practice_sig_payload(tenant: str, file_path: str, exp: int) -> str:
    return f"practice|{tenant}|{int(exp)}|{file_path}"


def _practice_sig(tenant: str, file_path: str, exp: int) -> str:
    payload = _practice_sig_payload(tenant, file_path, exp).encode('utf-8')
    return hmac.new(app.secret_key.encode('utf-8'), payload, hashlib.sha256).hexdigest()


def _verify_practice_sig(tenant: str, file_path: str, exp_value: str, sig: str) -> bool:
    try:
        exp = int(str(exp_value or '0'))
    except Exception:
        return False
    if exp < int(time.time()):
        return False
    expected = _practice_sig(tenant, file_path, exp)
    return bool(sig) and hmac.compare_digest(expected, str(sig))


def _practice_link_for_file(file_path: str, channel: str = 'stereo', expires_in: int = 900) -> str:
    tenant = _tenant_slug_from_request()
    exp = int(time.time()) + int(expires_in)
    sig = _practice_sig(tenant, file_path, exp)
    q = urllib.parse.urlencode({
        's3': file_path,
        'channel': channel,
        'exp': str(exp),
        'sig': sig,
    })
    return f"/practice?{q}"


def _jamulus_context() -> dict:
    tenant = _tenant_slug_from_request()
    port = TENANT_JSONRPC_PORTS.get(tenant, JSONRPC_PORT)
    recordings_dir = TENANT_RECORDING_DIRS.get(tenant) or RECORDINGS_DIR
    ctx = {
        'tenant': tenant,
        'host': JSONRPC_HOST,
        'port': int(port),
        'secret_file': JSONRPC_SECRET_FILE,
        'recordings_dir': recordings_dir,
    }

    try:
        proc = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=False)
        for line in proc.stdout.splitlines():
            raw = line.strip()
            if not raw or 'jamulus-headless' not in raw:
                continue
            if f"--jsonrpcport {ctx['port']}" not in raw:
                continue
            parts = raw.split(None, 1)
            if parts and parts[0].isdigit():
                ctx['pid'] = int(parts[0])
            m = re.search(r'(?:^|\s)-R\s+(\S+)', raw)
            if m:
                ctx['recordings_dir'] = m.group(1)
            break
    except Exception:
        pass

    return ctx


# ---------- HELPER: Jamulus PID ----------
def get_jamulus_pid(ctx: dict | None = None):
    """Return Jamulus server PID for the active tenant context."""
    ctx = ctx or _jamulus_context()
    pid = ctx.get('pid')
    if isinstance(pid, int) and pid > 0:
        return pid
    return None

# ---------- HELPER: Jamulus JSON-RPC ------------
def _read_jsonrpc_secret(ctx: dict | None = None) -> str | None:
    ctx = ctx or _jamulus_context()
    try:
        with open(ctx['secret_file'], "r") as f:
            return f.read().strip()
    except Exception:
        return None


def _jsonrpc_send_lines(lines: list[str], timeout: float = 2.0, *, ctx: dict | None = None) -> list[dict]:
    """Send newline-delimited JSON-RPC messages over TCP; returns decoded JSON responses."""
    ctx = ctx or _jamulus_context()
    data = ("\n".join(lines) + "\n").encode("utf-8")
    out: list[dict] = []
    with socket.create_connection((ctx['host'], int(ctx['port'])), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(data)
        buf = b""
        # Read up to len(lines) responses (auth + request)
        while len(out) < len(lines):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf and len(out) < len(lines):
                raw, buf = buf.split(b"\n", 1)
                raw = raw.strip()
                if not raw:
                    continue
                out.append(json.loads(raw.decode("utf-8")))
    return out


def jamulus_rpc_request(method: str, params: dict | None = None, *, ctx: dict | None = None) -> dict:
    ctx = ctx or _jamulus_context()
    secret = _read_jsonrpc_secret(ctx)
    if not secret:
        raise RuntimeError("JSON-RPC secret file missing/unreadable")

    auth = json.dumps({"id": 1, "jsonrpc": "2.0", "method": "jamulus/apiAuth", "params": {"secret": secret}})
    req = json.dumps({"id": 2, "jsonrpc": "2.0", "method": method, "params": params or {}})
    resps = _jsonrpc_send_lines([auth, req], ctx=ctx)
    if len(resps) < 2:
        raise RuntimeError("JSON-RPC: incomplete response")

    if resps[0].get("result") != "ok":
        raise RuntimeError(f"JSON-RPC auth failed: {resps[0]}")

    if "error" in resps[1]:
        raise RuntimeError(f"JSON-RPC error: {resps[1]['error']}")

    return resps[1]


# ---------- HELPER: recording state ------------
def jamulus_recording_enabled(ctx: dict | None = None) -> bool | None:
    """Return true/false if JSON-RPC is available; otherwise None."""
    ctx = ctx or _jamulus_context()
    try:
        resp = jamulus_rpc_request("jamulusserver/getRecorderStatus", ctx=ctx)
        result = resp.get("result") or {}
        return bool(result.get("enabled"))
    except Exception:
        return None


def _has_recent_recording_writes(active_window_seconds: int = 15, *, ctx: dict | None = None) -> bool:
    """Fallback heuristic: consider recording active if files were written recently in newest Jam-* dir."""
    ctx = ctx or _jamulus_context()
    recordings_dir = ctx.get('recordings_dir') or RECORDINGS_DIR
    try:
        if not os.path.isdir(recordings_dir):
            return False
        jam_dirs = sorted(
            (d for d in glob.glob(os.path.join(recordings_dir, "Jam-*")) if os.path.isdir(d)),
            key=lambda p: os.path.getmtime(p),
            reverse=True,
        )
        if not jam_dirs:
            return False
        newest = jam_dirs[0]
        newest_mtime = 0.0
        for root, _dirs, files in os.walk(newest):
            for fn in files:
                try:
                    mt = os.path.getmtime(os.path.join(root, fn))
                    newest_mtime = max(newest_mtime, mt)
                except FileNotFoundError:
                    pass
        if newest_mtime == 0.0:
            return False
        return (datetime.now().timestamp() - newest_mtime) <= active_window_seconds
    except Exception:
        return False


def _jamulus_toggle_recording(pid: int):
    """Toggle Jamulus recording using SIGUSR2. Requires passwordless sudo for kill."""
    return subprocess.run(["sudo", "-n", "kill", "-SIGUSR2", str(pid)], capture_output=True, text=True)


def _mark_recording_key_metronome_tainted(key: str | None):
    key = (key or '').strip()
    if not key:
        return
    try:
        with open(METRONOME_TAINT_MAP_CSV, 'a', newline='') as f:
            f.write(f"{key},1\n")
    except Exception:
        pass


def _active_recording_key() -> str:
    try:
        return Path(ACTIVE_RECORDING_KEY_FILE).read_text().strip()
    except Exception:
        return ''


# ---------- RECORDING TOGGLE ------------
@app.route("/toggle-recording", methods=["POST"])
def toggle_recording():
    ok, resp = _require_passcode()
    if not ok: return resp
    data  = request.get_json(force=True)
    name  = (data.get("session_name") or "").strip()
    action = (data.get("action") or "").strip().lower()
    include_injector = bool(data.get("include_injector", False))
    ctx = _jamulus_context()
    recording_key = _recording_map_key_now()

    # Persist user preference for uploader behavior
    try:
        Path(INCLUDE_INJECTOR_FLAG).write_text("1" if include_injector else "0")
    except Exception:
        pass
    try:
        with open(INCLUDE_INJECTOR_MAP_CSV, "a", newline="") as f:
            f.write(f"{recording_key},{1 if include_injector else 0}\n")
    except Exception:
        pass

    if action not in ("start", "stop"):
        return "Invalid action", 400

    pid = get_jamulus_pid(ctx)
    if not pid:
        return f"Jamulus server not running for tenant {ctx.get('tenant') or 'default'}", 500

    # Prefer JSON-RPC explicit start/stop + true enabled/disabled state.
    # If JSON-RPC isn't available yet, fall back to the file-write heuristic.
    is_enabled = jamulus_recording_enabled(ctx)
    if is_enabled is None:
        is_enabled = _has_recent_recording_writes(ctx=ctx)

    desired_active = (action == "start")

    # Pre-flight: if starting and a name is provided, enforce duplicate-name rule BEFORE starting recording
    if action == "start" and name:
        if name_used_recently(name, HOURS_BLOCK):
            msg = f"⏱ Name '{name}' was used in the last {HOURS_BLOCK} h"
            print(msg)
            return msg, 400

    if bool(is_enabled) == desired_active:
        # Already in the desired state; avoid accidentally toggling the wrong way.
        if action == "start" and name:
            # Still allow naming/logging without toggling.
            if name_used_recently(name, HOURS_BLOCK):
                msg = f"⏱ Name '{name}' was used in the last {HOURS_BLOCK} h"
                print(msg)
                return msg, 400
            save_session_name(name)
            log_session_name(name)
            with open(RECORDING_MAP_CSV, "a") as f:
                f.write(f"{_recording_map_key_now()},{name}\n")
        return "OK", 200

    # Trigger Jamulus.
    if jamulus_recording_enabled(ctx) is not None:
        try:
            method = "jamulusserver/startRecording" if action == "start" else "jamulusserver/stopRecording"
            jamulus_rpc_request(method, ctx=ctx)
        except Exception as e:
            return f"Failed to {action} recording via JSON-RPC: {e}", 500
    else:
        # Fallback: toggle
        res = _jamulus_toggle_recording(pid)
        if res.returncode != 0:
            msg = (res.stderr or res.stdout or "").strip()
            return f"Failed to toggle Jamulus recording (sudo/kill). {msg}", 500

    if action == "start":
        if name:
            if name_used_recently(name, HOURS_BLOCK):
                msg = f"⏱ Name '{name}' was used in the last {HOURS_BLOCK} h"
                print(msg)
                return msg, 400
            save_session_name(name)
            log_session_name(name)
            with open(RECORDING_MAP_CSV, "a") as f:
                f.write(f"{recording_key},{name}\n")
        Path(RECORDING_FLAG).write_text("ON")
        Path(SESSION_STATUS).write_text("ACTIVE")
        try:
            Path(ACTIVE_RECORDING_KEY_FILE).write_text(recording_key)
        except Exception:
            pass
        try:
            tenant = ctx.get('tenant')
            tb = _trackbot_url(tenant)
            code, body = _http_get(f"{tb}/api/metronome/status", timeout=2.0)
            data = json.loads(body) if body else {}
            if code and 200 <= code < 300 and data.get('running'):
                _mark_recording_key_metronome_tainted(recording_key)
        except Exception:
            pass
    else:  # stop
        for path in (RECORDING_FLAG, SESSION_STATUS):
            if os.path.exists(path):
                os.remove(path)
        try:
            Path(ACTIVE_RECORDING_KEY_FILE).unlink(missing_ok=True)
        except Exception:
            pass
    return "OK"

# ---------- STATUS ENDPOINTS ------------
@app.route("/get-status")
def get_status():
    ctx = _jamulus_context()
    enabled = jamulus_recording_enabled(ctx)
    if enabled is None:
        enabled = _has_recent_recording_writes(ctx=ctx)
    jamulus_running = bool(get_jamulus_pid(ctx))
    return jsonify({"recording_state": "ON" if enabled else "OFF", "jamulus_running": jamulus_running})

@app.route("/get-session-status")
def get_session_status():
    ctx = _jamulus_context()
    enabled = jamulus_recording_enabled(ctx)
    if enabled is None:
        enabled = _has_recent_recording_writes(ctx=ctx)
    return jsonify({"in_progress": bool(enabled)})


# ---------- TRACKBOT QUEUE / CONTROL ----------



@app.route('/metronome/start', methods=['POST'])
def metronome_start():
    ok, resp = _require_passcode()
    if not ok: return resp
    d = request.get_json(silent=True) or {}
    bpm = int(d.get('bpm') or 100)
    vol = float(d.get('vol') or 0.8)
    sig = (d.get('sig') or '4/4').strip()
    tenant = _tenant_slug_from_request()
    tb = _trackbot_url(tenant)
    url = f"{tb}/api/metronome/start?bpm={bpm}&vol={vol}&sig={urllib.parse.quote(sig, safe='')}"
    code, body = _http_get(url, timeout=5.0)
    try:
        ctx = _jamulus_context()
        is_recording = jamulus_recording_enabled(ctx)
        if is_recording is None:
            is_recording = _has_recent_recording_writes(ctx=ctx)
        if is_recording:
            _mark_recording_key_metronome_tainted(_active_recording_key())
    except Exception:
        pass
    try:
        data = json.loads(body) if body else {"ok": False}
    except Exception:
        data = {"ok": False, "error": body}
    if code and 200 <= code < 300 and data.get('ok'):
        return jsonify({"ok": True, "bpm": bpm, "vol": vol, "sig": sig})
    return jsonify({"ok": False, "upstream_code": code, "upstream": data}), 502

@app.route('/metronome/stop', methods=['POST'])
def metronome_stop():
    ok, resp = _require_passcode()
    if not ok: return resp
    tenant = _tenant_slug_from_request()
    tb = _trackbot_url(tenant)
    url = f"{tb}/api/metronome/stop"
    code, body = _http_get(url, timeout=5.0)
    try:
        data = json.loads(body) if body else {"ok": False}
    except Exception:
        data = {"ok": False, "error": body}
    if code and 200 <= code < 300 and data.get('ok'):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "upstream_code": code, "upstream": data}), 502

@app.route('/metronome/status')
def metronome_status():
    ok, resp = _require_passcode()
    if not ok: return resp
    tenant = _tenant_slug_from_request()
    tb = _trackbot_url(tenant)
    url = f"{tb}/api/metronome/status"
    code, body = _http_get(url, timeout=3.0)
    try:
        data = json.loads(body) if body else {"ok": False}
    except Exception:
        data = {"ok": False, "error": body}
    if code and 200 <= code < 300:
        return jsonify(data)
    return jsonify({"ok": False, "upstream_code": code, "upstream": data}), 502


def _support_parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception:
        return None


def _support_iso(dt):
    if not dt:
        return None
    if getattr(dt, 'tzinfo', None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _support_load_faq() -> dict:
    path = os.path.join(BASE_DIR, 'static', 'support-faq.json')
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        return {'version': 0, 'categories': []}
    if _support_faq_cache.get('data') is not None and _support_faq_cache.get('mtime') == mtime:
        return _support_faq_cache['data']
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {'version': 0, 'categories': []}
    _support_faq_cache['mtime'] = mtime
    _support_faq_cache['data'] = data
    return data


def _support_match_faq(text: str) -> dict | None:
    query = (text or '').strip().lower()
    if not query:
        return None
    q_tokens = [t for t in re.findall(r'[a-z0-9]+', query) if len(t) > 1]
    if not q_tokens:
        return None
    best = None
    best_score = 0
    for cat in (_support_load_faq().get('categories') or []):
        for item in (cat.get('questions') or []):
            hay = ' '.join([
                item.get('q') or '',
                item.get('a') or '',
                ' '.join(item.get('tags') or []),
                cat.get('title') or '',
                cat.get('id') or '',
            ]).lower()
            score = 0
            for tok in q_tokens:
                if tok in hay:
                    score += 1
                    if tok in ' '.join((item.get('tags') or [])).lower():
                        score += 2
                    if tok in (item.get('q') or '').lower():
                        score += 2
            if query in hay:
                score += 4
            if score > best_score:
                best_score = score
                best = {
                    'category_id': cat.get('id'),
                    'category_title': cat.get('title'),
                    'question': item.get('q'),
                    'answer': item.get('a'),
                    'tags': item.get('tags') or [],
                    'score': score,
                }
    return best if best_score >= 2 else None


def _support_first_recording_dt() -> datetime | None:
    now = time.time()
    cached = _support_first_recording_cache.get('value')
    if cached is not None and (now - float(_support_first_recording_cache.get('checked_at') or 0.0)) < 900:
        return cached

    earliest = None

    # Prefer authoritative S3 history for tenant age.
    prefix = f"vps/{LIBRARY_VPS_ID}/recordings/"
    try:
        s3 = boto3.client('s3', region_name=LIBRARY_AWS_REGION)
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=LIBRARY_S3_BUCKET, Prefix=prefix):
            for obj in (page.get('Contents') or []):
                key = obj.get('Key') or ''
                if not key or key.endswith('/'):
                    continue
                rel = key[len(prefix):] if key.startswith(prefix) else key
                if rel.startswith('temp/') or rel.startswith(f'{UPLOAD_SAVED_ROOT}/') or rel.startswith('.archived/'):
                    continue
                lm = obj.get('LastModified')
                if lm and (earliest is None or lm < earliest):
                    earliest = lm
    except Exception:
        pass

    # Fallback to local recording directory if S3 is unavailable.
    if earliest is None:
        try:
            for root, dirs, files in os.walk(RECORDINGS_DIR):
                dirs[:] = [d for d in dirs if d not in ('temp', UPLOAD_SAVED_ROOT, '.archived')]
                for fn in files:
                    p = os.path.join(root, fn)
                    try:
                        dt = datetime.fromtimestamp(os.path.getmtime(p), tz=timezone.utc)
                    except Exception:
                        continue
                    if earliest is None or dt < earliest:
                        earliest = dt
        except Exception:
            pass

    if earliest and getattr(earliest, 'tzinfo', None) is None:
        earliest = earliest.replace(tzinfo=timezone.utc)
    _support_first_recording_cache['checked_at'] = now
    _support_first_recording_cache['value'] = earliest
    return earliest


def _support_mode_context() -> dict:
    first_dt = _support_first_recording_dt()
    now_utc = datetime.now(timezone.utc)
    age_days = None
    welcome_until = None
    in_welcome = False
    if first_dt:
        age_days = max(0.0, (now_utc - first_dt.astimezone(timezone.utc)).total_seconds() / 86400.0)
        welcome_until = first_dt.astimezone(timezone.utc) + timedelta(days=SUPPORT_WELCOME_DAYS)
        in_welcome = now_utc < welcome_until
    return {
        'mode': 'welcome' if in_welcome else 'ai',
        'first_recording_at': _support_iso(first_dt),
        'age_days': round(age_days, 2) if age_days is not None else None,
        'welcome_days': SUPPORT_WELCOME_DAYS,
        'welcome_until_at': _support_iso(welcome_until),
        'phone': SUPPORT_WELCOME_PHONE,
        'pin': SUPPORT_WELCOME_PIN,
        'email_to': SUPPORT_EMAIL_TO,
        'email_bcc': SUPPORT_EMAIL_BCC,
        'email_enabled': bool(RESEND_API_KEY and SUPPORT_EMAIL_TO),
    }


def _support_append_event(token: str, role: str, message: str):
    if not token or token not in _support_sessions:
        return
    sess = _support_sessions[token]
    sess.setdefault('events', []).append({
        'ts': _support_iso(datetime.now(timezone.utc)),
        'role': role,
        'message': (message or '').strip(),
    })
    sess['events'] = sess['events'][-30:]


def _support_send_email(subject: str, html_body: str, text_body: str = '') -> tuple[bool, str]:
    if not RESEND_API_KEY:
        return False, 'resend_not_configured'
    if not SUPPORT_EMAIL_TO:
        return False, 'support_email_to_missing'
    payload = {
        'from': SUPPORT_EMAIL_FROM,
        'to': [SUPPORT_EMAIL_TO],
        'subject': subject,
        'html': html_body,
        'text': text_body or re.sub(r'<[^>]+>', ' ', html_body),
    }
    if SUPPORT_EMAIL_BCC:
        payload['bcc'] = [SUPPORT_EMAIL_BCC]
    req = urllib.request.Request(
        'https://api.resend.com/emails',
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {RESEND_API_KEY}',
            'Content-Type': 'application/json',
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode('utf-8', 'replace')
            return True, body
    except Exception as e:
        return False, str(e)


def _support_make_session(customer_id: str, server_id: str, room_name: str) -> dict:
    token = uuidlib.uuid4().hex
    _support_sessions[token] = {
        'customer_id': (customer_id or '').strip() or 'unknown-customer',
        'server_id': (server_id or '').strip() or LIBRARY_VPS_ID,
        'room_name': (room_name or '').strip(),
        'created_at': _now_local().isoformat(),
        'events': [],
        'support_mode': _support_mode_context(),
    }
    return {'ok': True, 'token': token, **_support_sessions[token]}


def _read_ops_health() -> dict:
    path = '/tmp/jambetter_health.json'
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'ok': False, 'error': 'no health snapshot yet', 'path': path}


def _run_healthcheck_now():
    try:
        subprocess.run(['/usr/bin/python3', '/home/nds/healthcheck.py'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except Exception:
        pass

def _support_status_payload() -> dict:
    """Stable status schema for ops dashboard polling.

    Back-compat: also includes a few legacy top-level keys used by older clients.
    """
    # Recorder state (JSON-RPC preferred)
    enabled = jamulus_recording_enabled()
    if enabled is None:
        enabled = _has_recent_recording_writes()

    # systemd service state (authoritative for start/stop)
    svc_state = 'unknown'
    svc_since = None
    try:
        p = subprocess.run(['systemctl', 'is-active', JAMULUS_SERVICE], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=4)
        svc_state = (p.stdout or '').strip() or 'unknown'
    except Exception:
        svc_state = 'unknown'

    try:
        p = subprocess.run(['systemctl', 'show', JAMULUS_SERVICE, '-p', 'ActiveEnterTimestamp', '--value'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=4)
        svc_since = (p.stdout or '').strip() or None
    except Exception:
        svc_since = None

    # JSON-RPC reachability (best effort)
    jsonrpc_reachable = False
    try:
        # if auth works, it's reachable
        _ = jamulus_rpc_request('jamulusserver/getServerParameters')
        jsonrpc_reachable = True
    except Exception:
        jsonrpc_reachable = False

    jamulus_running = (svc_state == 'active')

    # Lightweight server-side quality indicators (infrastructure health only)
    try:
        l1, l5, l15 = os.getloadavg()
    except Exception:
        l1, l5, l15 = (0.0, 0.0, 0.0)
    cores = os.cpu_count() or 1
    load_ratio = float(l1) / float(max(1, cores))

    if not jamulus_running:
        grade = 'red'
        label = 'Service down'
    elif load_ratio < 0.70:
        grade = 'green'
        label = 'Good'
    elif load_ratio < 1.10:
        grade = 'yellow'
        label = 'Busy'
    else:
        grade = 'red'
        label = 'Overloaded'

    load = {
        'load1': round(float(l1), 2),
        'load5': round(float(l5), 2),
        'load15': round(float(l15), 2),
        'cores': int(cores),
        'load_ratio': round(load_ratio, 2),
    }

    # Disk snapshot for ops dashboards (root filesystem by default)
    try:
        du = shutil.disk_usage('/')
        used_pct = (float(du.used) / float(max(1, du.total))) * 100.0
        disk = {
            'path': '/',
            'used_pct': round(used_pct, 1),
            'used_gb': round(float(du.used) / (1024**3), 2),
            'free_gb': round(float(du.free) / (1024**3), 2),
            'total_gb': round(float(du.total) / (1024**3), 2),
        }
    except Exception:
        disk = {'path': '/', 'error': 'disk_usage_failed'}

    quality = {
        'grade': grade,
        'label': label,
        'note': 'Server-side infrastructure health only; user network/device quality may still vary.',
    }

    utc_ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    # Stable schema (M1)
    payload = {
        'ok': True,
        'schema_version': 1,
        'server_id': LIBRARY_VPS_ID,
        'zone': SERVER_ZONE,
        'ts': utc_ts,
        'jamulus': {
            'service': {'state': svc_state, 'since': svc_since},
            'jsonrpc': {'reachable': bool(jsonrpc_reachable), 'port': int(JSONRPC_PORT)},
            'recorder': {'recording': bool(enabled)},
        },
        'quality': quality,
        'load': load,
        'disk': disk,
        'support_context': _support_mode_context(),

        # legacy fields (keep for existing clients)
        'jamulus_running': jamulus_running,
        'recording_state': 'ON' if enabled else 'OFF',
        'status_text': 'Online' if jamulus_running else 'Offline',
    }

    return payload



def _support_reply(intent: str, text: str = '', token: str = '') -> dict:
    intent = (intent or '').strip().lower()
    text = (text or '').strip()
    text_l = text.lower()
    mode = _support_mode_context()
    sess = _support_sessions.get(token) if token else None

    if not intent:
        if 'restart' in text_l:
            intent = 'restart'
        elif 'status' in text_l or 'availability' in text_l or 'up' in text_l:
            intent = 'status'
        elif 'audio' in text_l or 'hear' in text_l or 'latency' in text_l or 'connect' in text_l:
            intent = 'troubleshoot'
        elif 'human' in text_l or 'escalate' in text_l or 'help me' in text_l or 'call me' in text_l:
            intent = 'escalate'
        else:
            intent = 'help'

    if text:
        faq = _support_match_faq(text)
        if faq and intent in ('help', 'troubleshoot'):
            return {
                'ok': True,
                'intent': 'faq',
                'message': f"{faq['question']}\n\n{faq['answer']}",
                'data': {'faq': faq, 'support_context': mode},
                'quick_actions': ['status', 'troubleshoot', 'escalate']
            }

    if intent in ('status', 'availability'):
        st = _support_status_payload()
        return {
            'ok': True,
            'intent': 'status',
            'message': f"Server {st['server_id']} is {st['status_text']}. Recording is {st['recording_state']}.",
            'data': st,
            'quick_actions': ['restart', 'troubleshoot', 'escalate']
        }

    if intent == 'restart':
        tenant = _tenant_slug_from_request()
        tb = _trackbot_url(tenant)
        url = f"{tb}/api/jamulus/restart"
        code, body = _http_get(url, timeout=30.0)
        try:
            data = json.loads(body) if body else {'ok': False}
        except Exception:
            data = {'ok': False, 'error': body}
        ok = bool(code and 200 <= code < 300 and data.get('ok') is True)
        msg = 'Restart requested successfully. Please retry in 20-30 seconds.' if ok else 'Restart failed. I can escalate this to a human now.'
        return {'ok': ok, 'intent': 'restart', 'message': msg, 'data': {'upstream_code': code, 'upstream': data, 'support_context': mode}, 'quick_actions': ['status', 'escalate']}

    if intent == 'troubleshoot':
        steps = [
            '1) Confirm Jamulus server address/port are correct.',
            '2) Reconnect and verify your nickname/input device.',
            '3) Prefer wired Ethernet and a wired headset/mic combo.',
            '4) If audio stutters, increase buffer/latency slightly.',
            '5) If still failing, use Restart and rejoin after 30 seconds.'
        ]
        return {'ok': True, 'intent': 'troubleshoot', 'message': 'Try this quick checklist:\n' + '\n'.join(steps), 'data': {'support_context': mode}, 'quick_actions': ['status', 'restart', 'escalate']}

    if intent == 'escalate':
        if mode.get('mode') == 'welcome' and (mode.get('phone') or mode.get('pin')):
            bits = ['You are still in your first support week.']
            if mode.get('phone'):
                bits.append(f"Call {mode['phone']}")
            if mode.get('pin'):
                bits.append(f"and use PIN {mode['pin']}")
            msg = ' '.join(bits).strip() + '. I also captured your support context here.'
            return {'ok': True, 'intent': 'escalate', 'message': msg, 'data': {'support_context': mode}, 'quick_actions': ['status', 'troubleshoot']}

        st = _support_status_payload()
        transcript = '\n'.join([f"- [{e.get('ts')}] {e.get('role')}: {e.get('message')}" for e in (sess or {}).get('events', []) if e.get('message')])
        room_name = (sess or {}).get('room_name') or ''
        subject = f"JamBetter support request · {LIBRARY_VPS_ID}{(' · ' + room_name) if room_name else ''}"
        html_body = (
            f"<h2>JamBetter support request</h2>"
            f"<p><b>Server:</b> {LIBRARY_VPS_ID}<br>"
            f"<b>Room:</b> {room_name or '(not set)'}<br>"
            f"<b>Customer:</b> {(sess or {}).get('customer_id') or 'web-customer'}<br>"
            f"<b>First recording:</b> {mode.get('first_recording_at') or 'unknown'}<br>"
            f"<b>Tenant age (days):</b> {mode.get('age_days') if mode.get('age_days') is not None else 'unknown'}</p>"
            f"<pre>{json.dumps(st, indent=2)}</pre>"
            f"<h3>Transcript</h3><pre>{transcript or '(no transcript yet)'}</pre>"
        )
        ok, detail = _support_send_email(subject, html_body, text_body=f"Support request for {LIBRARY_VPS_ID}\n\nStatus:\n{json.dumps(st, indent=2)}\n\nTranscript:\n{transcript or '(no transcript yet)'}")
        if ok:
            return {'ok': True, 'intent': 'escalate', 'message': 'I sent your support request and system snapshot to the support team. They will follow up by email.', 'data': {'support_context': mode}, 'quick_actions': ['status']}
        return {'ok': False, 'intent': 'escalate', 'message': f"I tried to send support email but email delivery is not configured yet ({detail}).", 'data': {'support_context': mode}, 'quick_actions': ['status']}

    if mode.get('mode') == 'welcome' and (mode.get('phone') or mode.get('pin')):
        parts = [f"Welcome to JamBetter. For your first {mode.get('welcome_days')} days after your first recording, you can call direct support"]
        if mode.get('phone'):
            parts.append(mode['phone'])
        if mode.get('pin'):
            parts.append(f"with PIN {mode['pin']}")
        parts.append('I can also help right here with latency, connection, and website questions.')
        return {
            'ok': True,
            'intent': 'help',
            'message': ' '.join(parts),
            'data': {'support_context': mode},
            'quick_actions': ['status', 'troubleshoot', 'escalate']
        }

    return {
        'ok': True,
        'intent': 'help',
        'message': 'I can help with latency, website how-to questions, status, restarts, troubleshooting, or escalation by email if needed.',
        'data': {'support_context': mode},
        'quick_actions': ['status', 'restart', 'troubleshoot', 'escalate']
    }


@app.route('/api/support/session', methods=['POST'])
def api_support_session():
    ok, resp = _require_passcode()
    if not ok:
        return resp
    d = request.get_json(silent=True) or {}
    return jsonify(_support_make_session(d.get('customer_id') or '', d.get('server_id') or '', d.get('room_name') or ''))


@app.route('/api/support/message', methods=['POST'])
def api_support_message():
    ok, resp = _require_passcode()
    if not ok:
        return resp
    d = request.get_json(silent=True) or {}
    token = (d.get('token') or '').strip()
    if token and token not in _support_sessions:
        return jsonify({'ok': False, 'error': 'invalid session token'}), 400
    intent = d.get('intent') or ''
    text = d.get('text') or ''
    if text:
        _support_append_event(token, 'user', text)
    elif intent:
        _support_append_event(token, 'user', f'[{intent}]')
    out = _support_reply(intent, text, token=token)
    if out.get('message'):
        _support_append_event(token, 'bot', out.get('message') or '')
    if token in _support_sessions:
        _support_sessions[token]['support_mode'] = _support_mode_context()
    out['session'] = _support_sessions.get(token)
    return jsonify(out)


@app.route('/api/support/status', methods=['GET'])
def api_support_status():
    ok, resp = _require_passcode()
    if not ok:
        return resp
    ok, resp = _require_ops_token()
    if not ok:
        return resp
    return jsonify(_support_status_payload())


@app.route('/ops/health')
def ops_health():
    ok, resp = _require_passcode()
    if not ok:
        return resp
    _run_healthcheck_now()
    return jsonify(_read_ops_health())


@app.route('/ops/status', methods=['GET'])
def ops_status():
    """Alias for the stable ops polling schema.

    Rationale: the unified fleet dashboard prefers /ops/status when available,
    but older clients used /api/support/status.
    """
    ok, resp = _require_passcode()
    if not ok:
        return resp
    ok, resp = _require_ops_token()
    if not ok:
        return resp
    return jsonify(_support_status_payload())


@app.route('/ops')
def ops_dashboard():
    ok, resp = _require_passcode()
    if not ok:
        return resp
    return render_template('ops_dashboard.html')

@app.route('/wav/browse')
def wav_browse():
    ok, resp = _require_passcode()
    if not ok: return resp

    raw_path = request.args.get('path') or 'recordings/'
    area, sub = _safe_library_path_subpath(raw_path)
    if not area:
        return jsonify({"ok": False, "error": "path not allowed for this tenant"}), 403

    try:
        prefix = _lib_prefix(area, sub)
        result = _s3_list(prefix)
        dirs = list(result.get('dirs', []))
        files = result.get('files', [])

        # On tenant hosts, expose a stable shared library root and the tenant Saved
        # folder alongside the tenant-scoped recordings tree, even if Saved is empty.
        if area == 'recordings' and (sub or '') == ((_tenant_slug_from_request() or '') + '/'):
            if 'library' not in dirs:
                dirs.insert(0, 'library')
            if UPLOAD_SAVED_ROOT not in dirs:
                insert_at = 1 if dirs and dirs[0] == 'library' else 0
                dirs.insert(insert_at, UPLOAD_SAVED_ROOT)

        return jsonify({
            "ok": True,
            "path": f"{area}/" + (sub or ''),
            "dirs": dirs,
            "files": files
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/bot/hardreset-jamulus', methods=['POST'])
def bot_hardreset_jamulus():
    ok, resp = _require_passcode()
    if not ok: return resp
    tenant = _tenant_slug_from_request()
    tb = _trackbot_url(tenant)
    url = f"{tb}/api/jamulus/hardreset"
    code, body = _http_get(url, timeout=30.0)
    try:
        data = json.loads(body) if body else {"ok": False, "error": "empty response"}
    except Exception:
        data = {"ok": False, "error": body or "bad response"}
    if code and 200 <= code < 300 and data.get('ok') is True:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "upstream_code": code, "upstream": data}), 502

@app.route('/wav/queue', methods=['GET','POST','DELETE'])
def wav_queue():
    ok, resp = _require_passcode()
    if not ok: return resp
    tenant = _tenant_slug_from_request()
    if request.method == 'GET':
        return jsonify(_queue_read(tenant))
    if request.method == 'DELETE':
        _queue_clear(tenant)
        return 'OK'

    d = request.get_json(force=True) or {}
    # Expect either full relative path (within PipeDreamers), or any string the injector can resolve.
    wav = (d.get('file') or '').strip()
    if not wav:
        return 'Missing file', 400
    area, rel = _safe_library_file_path(wav)
    if _is_shared_library(area, rel):
        return jsonify({'ok': False, 'error': 'shared library files are practice-only'}), 403
    channel = (d.get('channel') or 'stereo').strip().lower()
    if channel not in ('stereo', 'left', 'right'):
        channel = 'stereo'
    q = _queue_read(tenant)
    q['file'] = wav
    q['channel'] = channel
    q['set_at'] = _now_local().isoformat()
    _queue_write(q, tenant)
    return 'OK'

@app.route('/wav/play-queued', methods=['POST'])
def wav_play_queued():
    ok, resp = _require_passcode()
    if not ok: return resp
    tenant = _tenant_slug_from_request()
    q = _queue_read(tenant)
    wav = (q.get('file') or '').strip()
    if not wav:
        return jsonify({'ok': True, 'skipped': True, 'reason': 'no queued wav'})

    # Tell tenant-specific TrackBot to play
    from urllib.parse import quote
    channel = (q.get('channel') or 'stereo').strip().lower()
    if channel not in ('stereo', 'left', 'right'):
        channel = 'stereo'
    tb = _trackbot_url(tenant)
    url = f"{tb}/play?file={quote(wav)}&channel={channel}"
    code, body = _http_get(url, timeout=20.0)
    if code and 200 <= code < 400:
        return jsonify({'ok': True, 'queued': wav, 'code': code})
    return jsonify({'ok': False, 'queued': wav, 'code': code, 'error': body}), 502

@app.route('/wav/stop', methods=['POST'])
def wav_stop():
    ok, resp = _require_passcode()
    if not ok: return resp
    tenant = _tenant_slug_from_request()
    tb = _trackbot_url(tenant)
    url = f"{tb}/stop"
    code, body = _http_get(url, timeout=3.0)
    if code and 200 <= code < 400:
        return jsonify({'ok': True, 'code': code})
    return jsonify({'ok': False, 'code': code, 'error': body}), 502


@app.route('/wav/auto-restart', methods=['GET', 'POST'])
def wav_auto_restart():
    ok, resp = _require_passcode()
    if not ok: return resp
    global _auto_restart_state
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        _auto_restart_state['enabled'] = bool(data.get('enabled', False))
        _auto_restart_state['silence_seconds'] = int(data.get('silence_seconds', 6))
        return jsonify({"ok": True, "enabled": _auto_restart_state['enabled'], "silence_seconds": _auto_restart_state['silence_seconds']})
    return jsonify({"ok": True, "enabled": _auto_restart_state['enabled'], "silence_seconds": _auto_restart_state['silence_seconds']})


@app.route('/wav/status')
def wav_status():
    ok, resp = _require_passcode()
    if not ok: return resp
    tenant = _tenant_slug_from_request()
    tb = _trackbot_url(tenant)
    url = f"{tb}/api/playback/status"
    code, body = _http_get(url, timeout=3.0)
    try:
        data = json.loads(body) if body else {"ok": False}
    except Exception:
        data = {"ok": False, "error": body}
    if code and 200 <= code < 300:
        return jsonify(data)
    return jsonify({"ok": False, "upstream_code": code, "upstream": data}), 502



@app.route("/wav/download")
def wav_download():
    ok, resp = _require_passcode()
    if not ok: return resp
    file_path = (request.args.get("file") or "").strip()
    if not file_path:
        return jsonify({"ok": False, "error": "missing file"}), 400
    area, rel = _safe_library_file_path(file_path)
    if not area:
        return jsonify({"ok": False, "error": "file not allowed for this tenant"}), 403
    if area == 'recordings' and rel.startswith('library/'):
        return jsonify({"ok": False, "error": "downloads disabled for shared library"}), 403
    key = _lib_prefix(area, '') + rel
    res = _s3_presign(key, expires=300, attachment=True)
    if not res.get("ok"):
        return jsonify(res), 403
    return redirect(res["url"])


@app.route("/wav/stream")
def wav_stream():
    ok, resp = _require_passcode()
    if not ok: return resp
    file_path = (request.args.get("file") or "").strip()
    if not file_path:
        return jsonify({"ok": False, "error": "missing file"}), 400
    area, rel = _safe_library_file_path(file_path)
    if not area:
        return jsonify({"ok": False, "error": "file not allowed for this tenant"}), 403
    if area == 'recordings' and rel.startswith('library/'):
        return jsonify({"ok": False, "error": "streaming disabled for shared library"}), 403
    key = _lib_prefix(area, '') + rel
    res = _s3_presign(key, expires=300, attachment=False)
    if not res.get("ok"):
        return jsonify(res), 403
    if (request.args.get("format") or "").strip().lower() == "json":
        return jsonify(res)
    return redirect(res["url"])


@app.route("/wav/practice-link")
def wav_practice_link():
    ok, resp = _require_passcode()
    if not ok: return resp
    file_path = (request.args.get("file") or "").strip()
    if not file_path:
        return jsonify({"ok": False, "error": "missing file"}), 400
    area, rel = _safe_library_file_path(file_path)
    if not _is_shared_library(area, rel):
        return jsonify({"ok": False, "error": "practice-only links are only for shared library files"}), 403
    channel = (request.args.get('channel') or 'stereo').strip().lower()
    if channel not in ('stereo', 'left', 'right'):
        channel = 'stereo'
    return jsonify({"ok": True, "url": _practice_link_for_file(file_path, channel=channel, expires_in=900)})


@app.route("/wav/practice-stream")
def wav_practice_stream():
    ok, resp = _require_passcode()
    if not ok: return resp
    file_path = (request.args.get("file") or "").strip()
    if not file_path:
        return jsonify({"ok": False, "error": "missing file"}), 400
    area, rel = _safe_library_file_path(file_path)
    if not _is_shared_library(area, rel):
        return jsonify({"ok": False, "error": "file not allowed for practice stream"}), 403
    tenant = _tenant_slug_from_request()
    exp = (request.args.get('exp') or '').strip()
    sig = (request.args.get('sig') or '').strip()
    if not _verify_practice_sig(tenant, file_path, exp, sig):
        return jsonify({"ok": False, "error": "invalid or expired practice token"}), 403
    key = _lib_prefix(area, '') + rel
    res = _s3_presign(key, expires=300, attachment=False)
    if not res.get("ok"):
        return jsonify(res), 403
    if (request.args.get("format") or "").strip().lower() == "json":
        return jsonify(res)
    return redirect(res["url"])


@app.route("/wav/download-folder")
def wav_download_folder():
    ok, resp = _require_passcode()
    if not ok:
        return resp

    area, sub = _safe_library_path_subpath(request.args.get('path') or '')
    if not area:
        return jsonify({"ok": False, "error": "invalid or missing folder path"}), 400
    if area == 'recordings' and (sub or '').startswith('library/'):
        return jsonify({"ok": False, "error": "downloads disabled for shared library"}), 403

    prefix = _lib_prefix(area, sub)
    try:
        s3 = boto3.client('s3', region_name=LIBRARY_AWS_REGION)
        token = None
        keys = []
        while True:
            kw = {'Bucket': LIBRARY_S3_BUCKET, 'Prefix': prefix}
            if token:
                kw['ContinuationToken'] = token
            resp = s3.list_objects_v2(**kw)
            for obj in resp.get('Contents') or []:
                key = obj.get('Key') or ''
                if not key or key.endswith('/'):
                    continue
                if key.startswith(prefix):
                    keys.append(key)
            if resp.get('IsTruncated'):
                token = resp.get('NextContinuationToken')
            else:
                break

        if not keys:
            return jsonify({"ok": False, "error": "folder is empty"}), 404

        tmp = tempfile.NamedTemporaryFile(prefix='jamulus_session_', suffix='.zip', delete=False)
        tmp_path = tmp.name
        tmp.close()

        with zipfile.ZipFile(tmp_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for key in keys:
                rel = key[len(prefix):]
                if not rel:
                    continue
                body = s3.get_object(Bucket=LIBRARY_S3_BUCKET, Key=key)['Body'].read()
                zf.writestr(rel, body)

        dl_name = _zip_filename_for_subpath(sub or area, fallback='session')
        response = send_file(tmp_path, as_attachment=True, download_name=dl_name, mimetype='application/zip')

        @response.call_on_close
        def _cleanup_tmp_zip():
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        return response
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/wav/upload", methods=["POST"])
@app.route("/wav/tmp-upload", methods=["POST"])
def wav_tmp_upload():
    ok, resp = _require_passcode()
    if not ok: return resp
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file provided"}), 400
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"ok": False, "error": "Empty filename"}), 400
    upload_tenant = _tenant_slug_from_request() or "shared"
    folder_name = _sanitize_folder_name(request.form.get('folder') or '')
    if not folder_name:
        return jsonify({"ok": False, "error": "Folder name is required"}), 400
    # Save to temp location
    tmp_dir = "/tmp/jamulus_uploads"
    os.makedirs(tmp_dir, exist_ok=True)
    import uuid
    filename = f"{uuid.uuid4().hex}_{f.filename.replace(' ', '_').replace('/', '_')}"
    filepath = os.path.join(tmp_dir, filename)
    f.save(filepath)
    # Upload to S3 in recordings/<tenant>/saved/<folder>/
    try:
        safe_folder = _ensure_saved_folder(upload_tenant, folder_name)
        s3 = boto3.client("s3", region_name=LIBRARY_AWS_REGION)
        key = f"vps/{LIBRARY_VPS_ID}/recordings/{upload_tenant}/{UPLOAD_SAVED_ROOT}/{safe_folder}/{filename}"
        s3.upload_file(filepath, LIBRARY_S3_BUCKET, key)
        os.remove(filepath)
        return jsonify({
            "ok": True,
            "file": f"recordings/{upload_tenant}/{UPLOAD_SAVED_ROOT}/{safe_folder}/{filename}",
            "folder": safe_folder,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/wav/create-folder', methods=['POST'])
def wav_create_folder():
    ok, resp = _require_passcode()
    if not ok:
        return resp
    tenant = _tenant_slug_from_request() or 'shared'
    data = request.get_json(silent=True) or {}
    folder_name = data.get('name') or data.get('folder') or ''
    try:
        safe_folder = _ensure_saved_folder(tenant, folder_name)
        return jsonify({
            'ok': True,
            'folder': safe_folder,
            'path': f'recordings/{tenant}/{UPLOAD_SAVED_ROOT}/{safe_folder}/',
        })
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

# ---------- Library (S3) API ----------


@app.route('/api/library/list')
def api_library_list():
    ok, resp = _require_passcode()
    if not ok: return resp

    area = (request.args.get('area') or 'recordings').strip().lower()
    sub = (request.args.get('prefix') or '').strip()
    prefix = _lib_prefix(area, sub)
    out = _s3_list(prefix)
    # Soft-delete: hide archived dirs at the root level
    if out.get('ok') and area in ('recordings','tracks') and (sub or '').strip() == '':
        archived = _list_archived_dirs(area)
        if archived and out.get('dirs'):
            out['dirs'] = [d for d in out['dirs'] if urllib.parse.quote(d, safe='') not in archived]
    return jsonify(out)


@app.route('/api/library/presign')
def api_library_presign():
    ok, resp = _require_passcode()
    if not ok: return resp

    key = (request.args.get('key') or '').strip()
    if not key:
        return jsonify({'ok': False, 'error': 'missing key'}), 400
    expires = int(request.args.get('expires') or '900')
    expires = max(60, min(expires, 3600))
    res = _s3_presign(key, expires=expires)
    return jsonify(res), (200 if res.get('ok') else 403)


@app.route('/api/library/archive', methods=['POST'])
def api_library_archive():
    ok, resp = _require_passcode()
    if not ok: return resp

    d = request.get_json(silent=True) or {}
    area = (d.get('area') or 'recordings').strip().lower()
    dir_name = (d.get('dir') or '').strip().strip('/')
    if area not in ('recordings','tracks'):
        area = 'recordings'
    if not dir_name:
        return jsonify({'ok': False, 'error': 'missing dir'}), 400

    # Only allow archiving immediate child directories at the area root (safety)
    if '/' in dir_name or '\\' in dir_name:
        return jsonify({'ok': False, 'error': 'dir must be a single folder name'}), 400

    try:
        s3 = boto3.client('s3', region_name=LIBRARY_AWS_REGION)
        key = _archived_marker_key(area, dir_name)
        s3.put_object(Bucket=LIBRARY_S3_BUCKET, Key=key, Body=b'')
        return jsonify({'ok': True, 'archived': dir_name})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/library/presign-put')
def api_library_presign_put():
    ok, resp = _require_passcode()
    if not ok: return resp

    area = (request.args.get('area') or 'tracks').strip().lower()
    prefix = (request.args.get('prefix') or '').strip()
    filename = (request.args.get('filename') or '').strip()
    content_type = (request.args.get('contentType') or 'application/octet-stream').strip()

    if not filename:
        return jsonify({'ok': False, 'error': 'missing filename'}), 400

    # basic filename sanitization
    filename = filename.replace('..', '').replace('\\', '/').split('/')[-1]
    if not filename:
        return jsonify({'ok': False, 'error': 'bad filename'}), 400

    # allowlist extensions
    low = filename.lower()
    if not (low.endswith('.mp3') or low.endswith('.wav') or low.endswith('.m4a') or low.endswith('.flac')):
        return jsonify({'ok': False, 'error': 'file type not allowed'}), 400

    # Prefer uploading to tracks by default; recordings can be enabled but keep it explicit
    if area not in ('tracks','recordings'):
        area = 'tracks'

    key = _lib_prefix(area, prefix) + filename

    # only allow within vps/<id>/(tracks|recordings)/
    allowed1 = f"vps/{LIBRARY_VPS_ID}/recordings/"
    allowed2 = f"vps/{LIBRARY_VPS_ID}/tracks/"
    if not (key.startswith(allowed1) or key.startswith(allowed2)):
        return jsonify({'ok': False, 'error': 'key not allowed'}), 403

    expires = int(request.args.get('expires') or '900')
    expires = max(60, min(expires, 3600))

    try:
        s3 = boto3.client('s3', region_name=LIBRARY_AWS_REGION)
        url = s3.generate_presigned_url(
            ClientMethod='put_object',
            Params={
                'Bucket': LIBRARY_S3_BUCKET,
                'Key': key,
                'ContentType': content_type,
            },
            ExpiresIn=int(expires),
        )
        return jsonify({'ok': True, 'url': url, 'key': key})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route("/toggle-lock", methods=["POST"])
def toggle_lock():
    ok, resp = _require_passcode()
    if not ok: return resp
    if request.get_json().get("password") != "your-password-here":
        return "Unauthorized", 403
    (os.remove if os.path.exists(LOCK_FILE) else lambda p: Path(p).write_text("LOCKED"))(LOCK_FILE)
    return "OK"

@app.route("/get-lock-status")
def get_lock_status():
    return jsonify({"is_locked": os.path.exists(LOCK_FILE)})


# ---------- OPS: Jamulus service controls (Fleet dashboard) ----------
JAMULUS_SERVICE = os.getenv('JAMULUS_SERVICE', 'jamulus-headless.service')

# Idempotent action journal/lock (M2 control-plane)
JAMULUS_ACTION_LOCK = os.getenv('JAMULUS_ACTION_LOCK', '/tmp/jambetter_jamulus_action.lock')
JAMULUS_ACTION_JOURNAL = os.getenv('JAMULUS_ACTION_JOURNAL', '/tmp/jambetter_actions.jsonl')
JAMULUS_ACTION_TIMEOUT_S = float(os.getenv('JAMULUS_ACTION_TIMEOUT_S', '25'))
SERVER_ZONE = os.getenv('JAMBETTER_ZONE', os.getenv('SERVER_ZONE', 'home')).strip() or 'home'



def _systemctl(action: str) -> tuple[int, str]:
    """Run systemctl via sudo (non-interactive)."""
    action = (action or '').strip().lower()
    if action not in ('start', 'stop', 'restart'):
        return 2, 'invalid action'
    try:
        p = subprocess.run(
            ['sudo', '-n', 'systemctl', action, JAMULUS_SERVICE],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=25,
        )
        return p.returncode, (p.stdout or '').strip()
    except Exception as e:
        return 1, str(e)




def _jamulus_action_read_journal() -> list[dict]:
    try:
        p = Path(JAMULUS_ACTION_JOURNAL)
        if not p.exists():
            return []
        rows = []
        for ln in p.read_text(encoding='utf-8').splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except Exception:
                continue
        return rows
    except Exception:
        return []


def _jamulus_action_find(request_id: str) -> dict | None:
    request_id = (request_id or '').strip()
    if not request_id:
        return None
    found = None
    for row in _jamulus_action_read_journal():
        if row.get('request_id') == request_id:
            found = row
    return found


def _jamulus_action_append(row: dict) -> None:
    try:
        os.makedirs(os.path.dirname(JAMULUS_ACTION_JOURNAL) or '/tmp', exist_ok=True)
        with open(JAMULUS_ACTION_JOURNAL, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:
        pass


def _jamulus_action_lock():
    os.makedirs(os.path.dirname(JAMULUS_ACTION_LOCK) or '/tmp', exist_ok=True)
    f = open(JAMULUS_ACTION_LOCK, 'a+', encoding='utf-8')
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    return f


def _systemctl_is_active() -> str:
    try:
        p = subprocess.run(['systemctl', 'is-active', JAMULUS_SERVICE], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=4)
        return (p.stdout or '').strip() or 'unknown'
    except Exception:
        return 'unknown'


@app.route('/api/jamulus/action/<request_id>', methods=['GET'])
def api_jamulus_action_status(request_id: str):
    ok, resp = _require_passcode()
    if not ok:
        return resp
    ok, resp = _require_ops_token()
    if not ok:
        return resp
    row = _jamulus_action_find(request_id)
    if not row:
        return jsonify({'ok': False, 'error': 'not found', 'request_id': request_id}), 404
    return jsonify({'ok': True, **row})

def _api_jamulus_action(action: str):
    ok, resp = _require_passcode()
    if not ok:
        return resp
    ok, resp = _require_ops_token()
    if not ok:
        return resp

    # Idempotency key: X-Request-Id header or JSON field.
    d = request.get_json(silent=True) or {}
    request_id = (request.headers.get('X-Request-Id') or d.get('request_id') or d.get('id') or '').strip() or uuidlib.uuid4().hex

    action = (action or '').strip().lower()
    if action not in ('start', 'stop', 'restart'):
        return jsonify({'ok': False, 'error': 'invalid action'}), 400

    # Fast path: if we've already done this request_id, return stored result.
    prev = _jamulus_action_find(request_id)
    if prev and prev.get('state') in ('done', 'failed'):
        return jsonify({'ok': True, **prev})

    # One action at a time per host.
    lock_f = _jamulus_action_lock()
    try:
        # Re-check after acquiring lock (prevents duplicate runs).
        prev = _jamulus_action_find(request_id)
        if prev and prev.get('state') in ('done', 'failed'):
            return jsonify({'ok': True, **prev})

        start_ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        _jamulus_action_append({'request_id': request_id, 'action': action, 'state': 'in_progress', 'ts': start_ts})

        # No-op semantics based on current state.
        cur = _systemctl_is_active()
        if action == 'start' and cur == 'active':
            row = {'request_id': request_id, 'action': action, 'state': 'done', 'result': 'noop', 'details': 'already active', 'ts': start_ts}
            _jamulus_action_append(row)
            return jsonify({'ok': True, **row})
        if action == 'stop' and cur in ('inactive', 'failed'):
            row = {'request_id': request_id, 'action': action, 'state': 'done', 'result': 'noop', 'details': f'already {cur}', 'ts': start_ts}
            _jamulus_action_append(row)
            return jsonify({'ok': True, **row})

        # Execute
        rc, out = _systemctl(action)
        done_ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        if rc == 0:
            row = {
                'request_id': request_id,
                'action': action,
                'state': 'done',
                'result': 'done',
                'service': JAMULUS_SERVICE,
                'output': out,
                'ts': done_ts,
            }
            _jamulus_action_append(row)
            return jsonify({'ok': True, **row})

        row = {
            'request_id': request_id,
            'action': action,
            'state': 'failed',
            'result': 'failed',
            'service': JAMULUS_SERVICE,
            'rc': rc,
            'output': out,
            'ts': done_ts,
        }
        _jamulus_action_append(row)
        return jsonify({'ok': False, **row}), 500
    finally:
        try:
            lock_f.close()
        except Exception:
            pass



@app.route('/api/jamulus/start', methods=['POST'])
def api_jamulus_start():
    return _api_jamulus_action('start')


@app.route('/api/jamulus/stop', methods=['POST'])
def api_jamulus_stop():
    return _api_jamulus_action('stop')


@app.route('/api/jamulus/restart', methods=['POST'])
def api_jamulus_restart():
    return _api_jamulus_action('restart')



@app.route('/bot/restart-jamulus', methods=['POST'])
def bot_restart_jamulus():
    ok, resp = _require_passcode()
    if not ok: return resp
    tenant = _tenant_slug_from_request()
    tb = _trackbot_url(tenant)
    url = f"{tb}/api/jamulus/restart"
    code, body = _http_get(url, timeout=30.0)
    try:
        data = json.loads(body) if body else {"ok": False, "error": "empty response"}
    except Exception:
        data = {"ok": False, "error": body or "bad response"}
    if code and 200 <= code < 300 and data.get('ok') is True:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "upstream_code": code, "upstream": data}), 502
# ---------- SESSION-NAME CRUD ----------
@app.route("/get-names")
def get_names():
    if not os.path.exists(SAVED_NAMES_CSV):
        return jsonify([])
    with open(SAVED_NAMES_CSV) as f:
        names = [ln.strip() for ln in f if ln.strip()]
    return jsonify([{"name": n} for n in sorted(names, key=str.lower)])

@app.route("/edit-name", methods=["POST"])
def edit_name():
    d = request.get_json(); old = d["old_name"].strip(); new = d["new_name"].strip()
    names = _load_names()
    if old in names:
        names.discard(old); names.add(new)
        _save_names(names)
    return "OK"

@app.route("/delete-name", methods=["POST"])
def delete_name():
    name = request.get_json()["name"].strip()
    names = _load_names()
    names.discard(name)
    _save_names(names)
    return "OK"

# ---------- INTERNAL UTILITIES ----------
def _load_names():
    if os.path.exists(SAVED_NAMES_CSV):
        with open(SAVED_NAMES_CSV) as f:
            return set(ln.strip() for ln in f if ln.strip())
    return set()

def _save_names(names):
    with open(SAVED_NAMES_CSV, "w") as f:
        for n in sorted(names, key=str.lower):
            f.write(f"{n}\n")

def save_session_name(name):
    names = _load_names()
    if name not in names:
        names.add(name)
        _save_names(names)

def name_used_recently(name, hours=10):
    if not os.path.exists(SESSION_LOG_JSN):
        return False
    try:
        with open(SESSION_LOG_JSN, "r") as f:
            log = json.load(f)
    except Exception as e:
        print(f"[LOG] JSON parse failed: {e}")
        return False

    now = _now_local()
    cutoff = now - timedelta(hours=hours)

    print(f"[LOG] Checking for recent use of '{name}' — now: {now}, cutoff: {cutoff}")

    for entry in log:
        if entry.get("name") == name:
            try:
                timestamp = datetime.fromisoformat(entry.get("timestamp"))
                # Older entries may be naive; assume local time.
                if timestamp.tzinfo is None and _LOCAL_TZ is not None:
                    timestamp = timestamp.replace(tzinfo=_LOCAL_TZ)
                print(f"[LOG] Found '{name}' entry at {timestamp}")
                if timestamp > cutoff:
                    print(f"[LOG] Name '{name}' used recently!")
                    return True
            except Exception as e:
                print(f"[LOG] Timestamp parse failed: {e}")
                continue
    return False

def log_session_name(name):
    entry = {"name": name, "timestamp": _now_local().isoformat()}
    log = []
    if os.path.exists(SESSION_LOG_JSN):
        try:
            with open(SESSION_LOG_JSN) as f:
                log = json.load(f)
        except Exception:
            pass
    log.append(entry)
    with open(SESSION_LOG_JSN, "w") as f:
        json.dump(log, f, indent=2)

# ---------- BOOTSTRAP ----------
if __name__ == "__main__":
    for p in (RECORDING_FLAG, SESSION_STATUS):
        if os.path.exists(p):
            os.remove(p)
    print("Working directory:", BASE_DIR)
    app.run("127.0.0.1", 5000)

@app.route('/wav/restart', methods=['POST'])
def wav_restart():
    ok, resp = _require_passcode()
    if not ok:
        return resp
    # Restart current playback on trackbot
    tenant = _tenant_slug_from_request()
    tb = _trackbot_url(tenant)
    url = f"{tb}/api/restart"
    code, body = _http_get(url, timeout=5.0)
    try:
        data = json.loads(body) if body else {"ok": False}
    except Exception:
        data = {"ok": False, "error": body}
    if code and 200 <= code < 300:
        return jsonify(data)
    return jsonify({"ok": False, "upstream_code": code, "upstream": data}), 502
