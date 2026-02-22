from flask import Flask, request, jsonify, render_template, make_response, session, redirect, send_file
import os, subprocess, json, glob, socket, threading, time, math, struct, tempfile, zipfile
import boto3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import urllib.parse
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
    """UTC key that matches Jamulus folder timestamp basis."""
    return datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')

app = Flask(__name__)

# Secret for signed session cookies (set env WEB_SESSION_SECRET to persist across restarts)
app.secret_key = os.getenv("WEB_SESSION_SECRET") or "b0d4488ec7feed6053da40a816518a8026518171275576c5ac64d41ea424de28"
# ---------- Auto-restart on silence state ----------
_auto_restart_state = {"enabled": False, "silence_seconds": 6}
_support_sessions: dict[str, dict] = {}

app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# ---------- WEB SECURITY (passcode) ----------
# Passcode gate disabled (we may reintroduce auth at the reverse-proxy layer later).
WEB_PASSCODE = ""


# ---------- CONSTANTS / PATHS ----------
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
RECORDING_FLAG  = "/tmp/jamulus_recording.flag"  # legacy UI flag (not authoritative)
LOCK_FILE       = "/tmp/jamulus_locked.flag"
SESSION_STATUS  = "/tmp/jamulus_session_active.flag"  # legacy UI flag (not authoritative)
INCLUDE_INJECTOR_FLAG = "/tmp/jamulus_include_injector.flag"

# Jamulus recordings parent dir
RECORDINGS_DIR  = "/var/lib/jamulus/recordings"

# Optional: Jamulus JSON-RPC (preferred; explicit start/stop + true recorder status)
JSONRPC_HOST = os.getenv("JAMULUS_JSONRPC_HOST", "127.0.0.1")
JSONRPC_PORT = int(os.getenv("JAMULUS_JSONRPC_PORT", "22100"))
JSONRPC_SECRET_FILE = os.getenv("JAMULUS_JSONRPC_SECRET_FILE", "/var/lib/jamulus/jsonrpc-secret.txt")

SAVED_NAMES_CSV = os.path.join(BASE_DIR, "saved_session_names.csv")
SESSION_LOG_JSN = os.path.join(BASE_DIR, "session_name_log.json")
RECORDING_MAP_CSV = "/home/nds/recording_name_map.csv"

HOURS_BLOCK     = 10
# --------------------------------------


# ---------- SOLO MODE (auto restart backing track on singer silence) ----------
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
# Keys live under: vps/<vps-id>/recordings/ and vps/<vps-id>/tracks/

# ---------- TRACKBOT (Injector WAV Playback) ----------
TRACKBOT_BASE_URL = os.getenv("TRACKBOT_BASE_URL", "http://172.16.31.3:8088").rstrip("/")
TRACKBOT_QUEUE_FILE = os.getenv("TRACKBOT_QUEUE_FILE", "/tmp/trackbot_queue.json")
SUPPORT_BOT_URL = os.getenv("SUPPORT_BOT_URL", "https://t.me/JamBetterBot").strip()


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


def _queue_read() -> dict:
    try:
        with open(TRACKBOT_QUEUE_FILE, "r") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def _queue_write(d: dict):
    tmp = TRACKBOT_QUEUE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, TRACKBOT_QUEUE_FILE)

def _queue_clear():
    try:
        os.remove(TRACKBOT_QUEUE_FILE)
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


def _safe_library_path_subpath(path_value: str) -> tuple[str, str] | tuple[None, None]:
    """Return (area, subpath) for a user path like recordings/foo/bar/.

    area in {recordings, tracks}; subpath is normalized relative path with trailing slash allowed.
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
    if sub and not sub.endswith('/'):
        sub += '/'
    return area, sub


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


def _s3_presign(key: str, expires: int = 900) -> dict:
    key = (key or "").lstrip("/")
    allowed1 = f"vps/{LIBRARY_VPS_ID}/recordings/"
    allowed2 = f"vps/{LIBRARY_VPS_ID}/tracks/"
    if not (key.startswith(allowed1) or key.startswith(allowed2)):
        return {"ok": False, "error": "key not allowed"}

    try:
        # Use boto3 presign so we can force attachment disposition.
        # Relies on the same ~/.aws credentials the server already has.
        s3 = boto3.client('s3', region_name=LIBRARY_AWS_REGION)
        filename = key.split('/')[-1]
        url = s3.generate_presigned_url(
            ClientMethod='get_object',
            Params={
                'Bucket': LIBRARY_S3_BUCKET,
                'Key': key,
                'ResponseContentDisposition': f'attachment; filename="{filename}"',
            },
            ExpiresIn=int(expires),
        )
        return {"ok": True, "url": url, "key": key}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------- HELPER: Jamulus PID ----------
def get_jamulus_pid():
    """Return Jamulus server PID (systemd first; fallback to pgrep)."""
    try:
        outp = subprocess.run(["systemctl", "show", "jamulus-headless.service", "-p", "MainPID", "--value"], capture_output=True, text=True).stdout.strip()
        if outp and outp.isdigit() and int(outp) > 0:
            return int(outp)
    except Exception:
        pass
    try:
        outp = subprocess.run(["pgrep", "-f", "/usr/bin/Jamulus -s"], capture_output=True, text=True).stdout.strip()
        return int(outp.splitlines()[0]) if outp else None
    except Exception:
        return None

# ---------- HELPER: Jamulus JSON-RPC ------------
def _read_jsonrpc_secret() -> str | None:
    try:
        with open(JSONRPC_SECRET_FILE, "r") as f:
            return f.read().strip()
    except Exception:
        return None


def _jsonrpc_send_lines(lines: list[str], timeout: float = 2.0) -> list[dict]:
    """Send newline-delimited JSON-RPC messages over TCP; returns decoded JSON responses."""
    data = ("\n".join(lines) + "\n").encode("utf-8")
    out: list[dict] = []
    with socket.create_connection((JSONRPC_HOST, JSONRPC_PORT), timeout=timeout) as s:
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


def jamulus_rpc_request(method: str, params: dict | None = None) -> dict:
    secret = _read_jsonrpc_secret()
    if not secret:
        raise RuntimeError("JSON-RPC secret file missing/unreadable")

    auth = json.dumps({"id": 1, "jsonrpc": "2.0", "method": "jamulus/apiAuth", "params": {"secret": secret}})
    req = json.dumps({"id": 2, "jsonrpc": "2.0", "method": method, "params": params or {}})
    resps = _jsonrpc_send_lines([auth, req])
    if len(resps) < 2:
        raise RuntimeError("JSON-RPC: incomplete response")

    if resps[0].get("result") != "ok":
        raise RuntimeError(f"JSON-RPC auth failed: {resps[0]}")

    if "error" in resps[1]:
        raise RuntimeError(f"JSON-RPC error: {resps[1]['error']}")

    return resps[1]


# ---------- HELPER: recording state ------------
def jamulus_recording_enabled() -> bool | None:
    """Return true/false if JSON-RPC is available; otherwise None."""
    try:
        resp = jamulus_rpc_request("jamulusserver/getRecorderStatus")
        result = resp.get("result") or {}
        return bool(result.get("enabled"))
    except Exception:
        return None


def _has_recent_recording_writes(active_window_seconds: int = 15) -> bool:
    """Fallback heuristic: consider recording active if files were written recently in newest Jam-* dir."""
    try:
        if not os.path.isdir(RECORDINGS_DIR):
            return False
        jam_dirs = sorted(
            (d for d in glob.glob(os.path.join(RECORDINGS_DIR, "Jam-*")) if os.path.isdir(d)),
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


# ---------- RECORDING TOGGLE ------------
@app.route("/toggle-recording", methods=["POST"])
def toggle_recording():
    ok, resp = _require_passcode()
    if not ok: return resp
    data  = request.get_json(force=True)
    name  = (data.get("session_name") or "").strip()
    action = (data.get("action") or "").strip().lower()
    include_injector = bool(data.get("include_injector", False))

    # Persist user preference for uploader behavior
    try:
        Path(INCLUDE_INJECTOR_FLAG).write_text("1" if include_injector else "0")
    except Exception:
        pass

    if action not in ("start", "stop"):
        return "Invalid action", 400

    pid = get_jamulus_pid()
    if not pid:
        return "Jamulus server not running", 500

    # Prefer JSON-RPC explicit start/stop + true enabled/disabled state.
    # If JSON-RPC isn't available yet, fall back to the file-write heuristic.
    is_enabled = jamulus_recording_enabled()
    if is_enabled is None:
        is_enabled = _has_recent_recording_writes()

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
    if jamulus_recording_enabled() is not None:
        try:
            method = "jamulusserver/startRecording" if action == "start" else "jamulusserver/stopRecording"
            jamulus_rpc_request(method)
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
                f.write(f"{_recording_map_key_now()},{name}\n")
        Path(RECORDING_FLAG).write_text("ON")
        Path(SESSION_STATUS).write_text("ACTIVE")
    else:  # stop
        for path in (RECORDING_FLAG, SESSION_STATUS):
            if os.path.exists(path):
                os.remove(path)
    return "OK"

# ---------- STATUS ENDPOINTS ------------
@app.route("/get-status")
def get_status():
    enabled = jamulus_recording_enabled()
    if enabled is None:
        enabled = _has_recent_recording_writes()
    # Check if Jamulus server is running
    jamulus_running = False
    try:
        result = subprocess.run(["pgrep", "-x", "jamulus"], capture_output=True)
        jamulus_running = result.returncode == 0
    except Exception:
        pass
    return jsonify({"recording_state": "ON" if enabled else "OFF", "jamulus_running": jamulus_running})

@app.route("/get-session-status")
def get_session_status():
    enabled = jamulus_recording_enabled()
    if enabled is None:
        enabled = _has_recent_recording_writes()
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
    url = f"{TRACKBOT_BASE_URL}/api/metronome/start?bpm={bpm}&vol={vol}&sig={urllib.parse.quote(sig, safe='')}"
    code, body = _http_get(url, timeout=5.0)
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
    url = f"{TRACKBOT_BASE_URL}/api/metronome/stop"
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
    url = f"{TRACKBOT_BASE_URL}/api/metronome/status"
    code, body = _http_get(url, timeout=3.0)
    try:
        data = json.loads(body) if body else {"ok": False}
    except Exception:
        data = {"ok": False, "error": body}
    if code and 200 <= code < 300:
        return jsonify(data)
    return jsonify({"ok": False, "upstream_code": code, "upstream": data}), 502


def _support_make_session(customer_id: str, server_id: str, room_name: str) -> dict:
    token = uuidlib.uuid4().hex
    _support_sessions[token] = {
        'customer_id': (customer_id or '').strip() or 'unknown-customer',
        'server_id': (server_id or '').strip() or LIBRARY_VPS_ID,
        'room_name': (room_name or '').strip(),
        'created_at': _now_local().isoformat(),
    }
    return {'ok': True, 'token': token, **_support_sessions[token]}


def _support_status_payload() -> dict:
    enabled = jamulus_recording_enabled()
    if enabled is None:
        enabled = _has_recent_recording_writes()
    jamulus_running = False
    try:
        result = subprocess.run(['pgrep', '-x', 'jamulus'], capture_output=True)
        jamulus_running = result.returncode == 0
    except Exception:
        pass
    return {
        'ok': True,
        'server_id': LIBRARY_VPS_ID,
        'jamulus_running': jamulus_running,
        'recording_state': 'ON' if enabled else 'OFF',
        'status_text': 'Online' if jamulus_running else 'Offline'
    }


def _support_reply(intent: str, text: str = '') -> dict:
    intent = (intent or '').strip().lower()
    text_l = (text or '').strip().lower()
    if not intent:
        if 'restart' in text_l:
            intent = 'restart'
        elif 'status' in text_l or 'availability' in text_l or 'up' in text_l:
            intent = 'status'
        elif 'audio' in text_l or 'hear' in text_l or 'latency' in text_l or 'connect' in text_l:
            intent = 'troubleshoot'
        elif 'human' in text_l or 'escalate' in text_l or 'help me' in text_l:
            intent = 'escalate'
        else:
            intent = 'help'

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
        url = f"{TRACKBOT_BASE_URL}/api/jamulus/restart"
        code, body = _http_get(url, timeout=30.0)
        try:
            data = json.loads(body) if body else {'ok': False}
        except Exception:
            data = {'ok': False, 'error': body}
        ok = bool(code and 200 <= code < 300 and data.get('ok') is True)
        msg = 'Restart requested successfully. Please retry in 20-30 seconds.' if ok else 'Restart failed. I can escalate this to a human now.'
        return {'ok': ok, 'intent': 'restart', 'message': msg, 'data': {'upstream_code': code, 'upstream': data}, 'quick_actions': ['status', 'escalate']}

    if intent == 'troubleshoot':
        steps = [
            '1) Confirm Jamulus server address/port are correct.',
            '2) Reconnect and verify your nickname/input device.',
            '3) If audio stutters, increase buffer/latency slightly.',
            '4) If still failing, use Restart and rejoin after 30 seconds.'
        ]
        return {'ok': True, 'intent': 'troubleshoot', 'message': 'Try this quick checklist:\n' + '\n'.join(steps), 'quick_actions': ['status', 'restart', 'escalate']}

    if intent == 'escalate':
        return {'ok': True, 'intent': 'escalate', 'message': 'Escalation requested. A human operator will review this server issue shortly.', 'quick_actions': ['status']}

    return {
        'ok': True,
        'intent': 'help',
        'message': 'I can help with: status, availability, restart, troubleshooting, or escalation.',
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
    out = _support_reply(intent, text)
    out['session'] = _support_sessions.get(token)
    return jsonify(out)


@app.route('/api/support/status', methods=['GET'])
def api_support_status():
    ok, resp = _require_passcode()
    if not ok:
        return resp
    return jsonify(_support_status_payload())

@app.route('/wav/browse')
def wav_browse():
    ok, resp = _require_passcode()
    if not ok: return resp
    # Use library API directly
    sub = (request.args.get('path') or '').lstrip('/')
    area = 'tracks' if sub.startswith('tracks/') else 'recordings'
    prefix = sub.replace('tracks/', '').replace('recordings/', '')
    
    try:
        prefix = f"vps/{LIBRARY_VPS_ID}/{area}/" + prefix
        result = _s3_list(prefix)
        return jsonify({"ok": True, "dirs": result.get('dirs', []), "files": result.get('files', [])})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/bot/hardreset-jamulus', methods=['POST'])
def bot_hardreset_jamulus():
    ok, resp = _require_passcode()
    if not ok: return resp
    url = f"{TRACKBOT_BASE_URL}/api/jamulus/hardreset"
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
    if request.method == 'GET':
        return jsonify(_queue_read())
    if request.method == 'DELETE':
        _queue_clear()
        return 'OK'

    d = request.get_json(force=True) or {}
    # Expect either full relative path (within PipeDreamers), or any string the injector can resolve.
    wav = (d.get('file') or '').strip()
    if not wav:
        return 'Missing file', 400
    q = _queue_read()
    q['file'] = wav
    q['set_at'] = _now_local().isoformat()
    _queue_write(q)
    return 'OK'

@app.route('/wav/play-queued', methods=['POST'])
def wav_play_queued():
    ok, resp = _require_passcode()
    if not ok: return resp
    q = _queue_read()
    wav = (q.get('file') or '').strip()
    if not wav:
        return jsonify({'ok': True, 'skipped': True, 'reason': 'no queued wav'})

    # Tell injector TrackBot to play
    from urllib.parse import quote
    url = f"{TRACKBOT_BASE_URL}/play?file={quote(wav)}"
    code, body = _http_get(url, timeout=20.0)
    if code and 200 <= code < 400:
        return jsonify({'ok': True, 'queued': wav, 'code': code})
    return jsonify({'ok': False, 'queued': wav, 'code': code, 'error': body}), 502

@app.route('/wav/stop', methods=['POST'])
def wav_stop():
    ok, resp = _require_passcode()
    if not ok: return resp
    url = f"{TRACKBOT_BASE_URL}/stop"
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
    url = f"{TRACKBOT_BASE_URL}/api/playback/status"
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
    key = f"vps/{LIBRARY_VPS_ID}/{file_path}"
    res = _s3_presign(key, expires=300)
    if not res.get("ok"):
        return jsonify(res), 403
    return redirect(res["url"])


@app.route("/wav/download-folder")
def wav_download_folder():
    ok, resp = _require_passcode()
    if not ok:
        return resp

    area, sub = _safe_library_path_subpath(request.args.get('path') or '')
    if not area:
        return jsonify({"ok": False, "error": "invalid or missing folder path"}), 400

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


@app.route("/wav/tmp-upload", methods=["POST"])
def wav_tmp_upload():
    ok, resp = _require_passcode()
    if not ok: return resp
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file provided"}), 400
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"ok": False, "error": "Empty filename"}), 400
    # Save to temp location
    tmp_dir = "/tmp/jamulus_uploads"
    os.makedirs(tmp_dir, exist_ok=True)
    import uuid
    filename = f"{uuid.uuid4().hex}_{f.filename.replace(' ', '_').replace('/', '_')}"
    filepath = os.path.join(tmp_dir, filename)
    f.save(filepath)
    # Upload to S3 in recordings/temp/
    key = f"vps/{LIBRARY_VPS_ID}/recordings/temp/{filename}"
    try:
        s3 = boto3.client("s3", region_name=LIBRARY_AWS_REGION)
        s3.upload_file(filepath, LIBRARY_S3_BUCKET, key)
        os.remove(filepath)
        return jsonify({"ok": True, "file": f"recordings/temp/{filename}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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



@app.route('/bot/restart-jamulus', methods=['POST'])
def bot_restart_jamulus():
    ok, resp = _require_passcode()
    if not ok: return resp
    url = f"{TRACKBOT_BASE_URL}/api/jamulus/restart"
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
    if not ok: return resp
    # Restart current playback on trackbot
    url = f"{TRACKBOT_BASE_URL}/api/restart"
    code, body = _http_get(url, timeout=5.0)
    try:
        data = json.loads(body) if body else {ok: False}
    except Exception:
        data = {ok: False, error: body}
    if code and 200 <= code < 300:
        return jsonify(data)
    return jsonify({ok: False, upstream_code: code, upstream: data}), 502
