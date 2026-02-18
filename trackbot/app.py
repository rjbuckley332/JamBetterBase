#!/usr/bin/env python3
# TrackBot with Manual Arm Auto-Restart
# User clicks "Arm" after starting to sing, THEN silence detection activates

import html
import os
import shlex
import subprocess
import threading
import urllib.parse
import json
import wave
import math
import time
import struct
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = os.environ.get('TRACKBOT_HOST', '127.0.0.1')
PORT = int(os.environ.get('TRACKBOT_PORT', '8088'))
RCLONE_REMOTE = os.environ.get('TRACKBOT_RCLONE_REMOTE', 'gdrive2:1PipeDreamers Music and Recordings.lnk')
RCLONE_FLAGS = shlex.split(os.environ.get('TRACKBOT_RCLONE_FLAGS', '--config /home/nds/.config/rclone/rclone.conf'))
JAMULUS_IN_L = os.environ.get('TRACKBOT_JAMULUS_IN_L', 'Jamulus Injector-bot:input left')
JAMULUS_IN_R = os.environ.get('TRACKBOT_JAMULUS_IN_R', 'Jamulus Injector-bot:input right')
PLAYER_NAME = os.environ.get('TRACKBOT_PLAYER_NAME', 'trackbot_player')

state = {
    'lock': threading.Lock(),
    'proc': None,
    'now': None,
    'last_err': None,
}

# Metronome state
metronome_lock = threading.Lock()
metronome_stop = threading.Event()
metronome_thread = None
metronome_bpm = None
metronome_proc = None
metronome_volume = 0.25
metronome_seq = 0

# Debounce metronome updates
metronome_pending = {'bpm': None, 'vol': None}
metronome_debounce_thread = None
metronome_debounce_evt = threading.Event()
metronome_debounce_delay = 0.20

# Manual Arm Auto-Restart State
auto_restart_armed = False
auto_restart_lock = threading.Lock()
auto_restart_thread = None
auto_restart_stop = threading.Event()

SILENCE_DURATION_SECONDS = 6
CHECK_INTERVAL = 0.5
# Fixed threshold: -5 dB (well above -9.5 dB backing track)
# When user sings, level should go above -5 dB
# When user stops, level drops below -5 dB → silence detected
FIXED_THRESHOLD_DB = -3

def _pw_env():
    env = os.environ.copy()
    env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    return env

def get_listenerbot_outputs():
    """Find ListenerBot JACK output ports (for silence detection)."""
    result = subprocess.run(['jack_lsp'], capture_output=True, text=True, env=_pw_env())
    if result.returncode != 0:
        return None, None
    
    left_port = None
    right_port = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if 'ListenerBot:output left' in line or 'ListenerBot:out_1' in line or 'ListenerBot:output_FL' in line:
            left_port = line
        elif 'ListenerBot:output right' in line or 'ListenerBot:out_2' in line or 'ListenerBot:output_FR' in line:
            right_port = line
    
    return left_port, right_port

def calculate_rms_db(data_bytes):
    """Calculate RMS dB from 16-bit PCM data."""
    if len(data_bytes) < 2:
        return -100.0
    
    count = len(data_bytes) // 2
    if count == 0:
        return -100.0
    
    sum_squares = 0
    for i in range(0, len(data_bytes) - 1, 2):
        sample = struct.unpack('<h', data_bytes[i:i+2])[0]
        sum_squares += sample * sample
    
    rms = math.sqrt(sum_squares / count)
    if rms <= 0:
        return -100.0
    
    db = 20 * math.log10(rms / 32768.0)
    return db

def silence_monitor_thread():
    """Monitor for silence and auto-restart when armed."""
    global auto_restart_armed
    
    # Wait for ListenerBot ports (JACK client that hears everything)
    left_port = None
    for _ in range(60):
        left_port, _ = get_listenerbot_outputs()
        if left_port:
            break
        time.sleep(1)
    
    if not left_port:
        print("[AutoRestart] Could not find ListenerBot ports - make sure listenerbot.service is running")
        return
    
    print(f"[AutoRestart] Monitoring ListenerBot port: {left_port}")
    
    state_singing = False
    silence_start = None
    
    print(f"[AutoRestart] Monitor started. Threshold: {FIXED_THRESHOLD_DB} dB")
    
    while not auto_restart_stop.is_set():
        with auto_restart_lock:
            is_armed = auto_restart_armed
        
        if not is_armed:
            time.sleep(CHECK_INTERVAL)
            state_singing = False  # Reset when disarmed
            silence_start = None
            continue
        
        try:
            # Record short sample using jack_capture
            tmp_path = f"/tmp/autorestart_sample_{os.getpid()}.wav"
            cmd = [
                'jack_capture',
                '-d', '0.5',  # 0.5 seconds
                '-f', 'wav',
                '--port', left_port,
                tmp_path
            ]
            
            proc = subprocess.run(cmd, capture_output=True, timeout=3, env=_pw_env())
            
            if proc.returncode == 0 and os.path.exists(tmp_path):
                with open(tmp_path, 'rb') as f:
                    f.seek(44)  # Skip WAV header
                    data = f.read()
                os.remove(tmp_path)
                
                db = calculate_rms_db(data)
                
                # Use fixed threshold
                threshold = FIXED_THRESHOLD_DB
                
                # State machine
                if not state_singing:
                    if db > threshold:
                        print(f"[AutoRestart] 🎤 SINGING DETECTED: {db:.1f} dB (threshold: {threshold:.1f} dB)")
                        state_singing = True
                        silence_start = None
                else:
                    if db <= threshold:
                        if silence_start is None:
                            print(f"[AutoRestart] 🤫 Silence started: {db:.1f} dB")
                            silence_start = time.time()
                        else:
                            elapsed = time.time() - silence_start
                            if elapsed >= SILENCE_DURATION_SECONDS:
                                print(f"[AutoRestart] ⏰ Silence for {elapsed:.1f}s - RESTARTING")
                                # Restart track
                                with state['lock']:
                                    now = state['now']
                                if now:
                                    stop_playback()
                                    time.sleep(0.1)
                                    start_playback(now)
                                # Reset state
                                state_singing = False
                                silence_start = None
                                # Disarm after restart
                                with auto_restart_lock:
                                    auto_restart_armed = False
                                print("[AutoRestart] ✅ Disarmed after restart")
                    else:
                        if silence_start is not None:
                            print(f"[AutoRestart] 🎤 Singing resumed: {db:.1f} dB")
                        silence_start = None
            
        except Exception as e:
            print(f"[AutoRestart] Error: {e}")
        
        time.sleep(CHECK_INTERVAL)

def start_silence_monitor():
    """Start the silence monitor thread."""
    global auto_restart_thread, auto_restart_stop
    auto_restart_stop.clear()
    auto_restart_thread = threading.Thread(target=silence_monitor_thread, daemon=True)
    auto_restart_thread.start()
    print("[AutoRestart] Monitor thread started")

def list_wavs(limit=200):
    cmd = ['rclone','lsf','-R',RCLONE_REMOTE,'--files-only','--include','*.wav','--include','*.WAV','--include','*.mp3','--include','*.MP3'] + RCLONE_FLAGS
    p = subprocess.run(cmd, text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or 'rclone failed')
    lines = [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]
    return lines[:limit]

def stop_playback():
    with state['lock']:
        proc = state['proc']
        state['proc'] = None
        state['now'] = None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

def start_playback(relpath):
    """Start playback into Jamulus via PipeWire."""
    stop_playback()
    
    # Note: We don't disarm here - user may have armed before playing
    # The arm state persists until restart or manual disarm

    full = f"{RCLONE_REMOTE.rstrip('/')}/{relpath.lstrip('/')}"

    import hashlib, tempfile
    from pathlib import Path

    ext = (relpath.rsplit('.', 1)[-1].lower() if '.' in relpath else 'wav')
    h = hashlib.sha1(full.encode('utf-8')).hexdigest()[:10]
    tmpdir = Path(tempfile.gettempdir()) / 'trackbot'
    tmpdir.mkdir(parents=True, exist_ok=True)
    src_path = tmpdir / f"src_{h}.{ext}"
    wav_path = tmpdir / f"play_{h}.wav"

    rclone_cmd = ['rclone','cat',full] + RCLONE_FLAGS
    with open(src_path, 'wb') as f:
        r = subprocess.run(rclone_cmd, stdout=f, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b'').decode('utf-8', errors='replace') or 'rclone cat failed')

    ffmpeg_cmd = ['ffmpeg','-hide_banner','-loglevel','error','-y','-i', str(src_path), '-af','volume=0.5','-ac','2','-ar','48000', str(wav_path)]
    d = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
    if d.returncode != 0:
        raise RuntimeError(d.stderr.strip() or 'ffmpeg decode failed')

    play_cmd = ['pw-cat', '-p', '--target', '0', '--latency', '200ms', '--volume', '0.5',
                '--properties', 'node.name=trackbot_player', '--properties', 'media.role=Music', str(wav_path)]
    proc = subprocess.Popen(play_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=_pw_env())

    # Link to Jamulus
    out_l = None
    out_r = None
    for _ in range(60):
        out = subprocess.run(['pw-link', '-o'], capture_output=True, text=True, env=_pw_env())
        if out.returncode == 0:
            for ln in out.stdout.splitlines():
                ln = ln.strip()
                if ln == 'trackbot_player:output_FL':
                    out_l = ln
                elif ln == 'trackbot_player:output_FR':
                    out_r = ln
        if out_l and out_r:
            break
        time.sleep(0.1)

    if out_l and out_r:
        subprocess.run(['pw-link', out_l, 'Jamulus Injector-bot:input left'], capture_output=True, text=True, env=_pw_env())
        subprocess.run(['pw-link', out_r, 'Jamulus Injector-bot:input right'], capture_output=True, text=True, env=_pw_env())

    with state['lock']:
        state['proc'] = proc
        state['now'] = relpath
    
    # Auto-arm after 6 seconds (gives time to start singing)
    def delayed_arm():
        time.sleep(6)
        global auto_restart_armed
        with auto_restart_lock:
            auto_restart_armed = True
        print("[AutoRestart] 🔔 AUTO-ARMED after 6 seconds!")
    
    threading.Thread(target=delayed_arm, daemon=True).start()
    print("[AutoRestart] Will auto-arm in 6 seconds...")

def run(cmd, stdin=None, check=True):
    p = subprocess.run(cmd, input=stdin, text=False if stdin is not None else True, capture_output=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {cmd}\n{p.stderr.decode() if isinstance(p.stderr, (bytes, bytearray)) else p.stderr}")
    return p

class Handler(BaseHTTPRequestHandler):

    def _send(self, code, body, ctype='text/html; charset=utf-8'):
        body_b = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body_b)))
        self.end_headers()
        self.wfile.write(body_b)

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
            
            if u.path == '/api/autorestart/arm':
                global auto_restart_armed
                with auto_restart_lock:
                    auto_restart_armed = True
                print("[HTTP] Auto-restart ARMED")
                self._send(200, json.dumps({'ok': True, 'armed': True}), ctype='application/json')
                return
            
            if u.path == '/api/autorestart/disarm':
                with auto_restart_lock:
                    auto_restart_armed = False
                print("[HTTP] Auto-restart DISARMED")
                self._send(200, json.dumps({'ok': True, 'armed': False}), ctype='application/json')
                return
            
            if u.path == '/api/autorestart/status':
                with auto_restart_lock:
                    is_armed = auto_restart_armed
                self._send(200, json.dumps({'ok': True, 'armed': is_armed}), ctype='application/json')
                return

            if u.path == '/api/restart':
                with state['lock']:
                    now = state['now']
                if now:
                    stop_playback()
                    time.sleep(0.1)
                    start_playback(now)
                    self._send(200, json.dumps({'ok': True, 'restarted': now}), ctype='application/json')
                else:
                    self._send(400, json.dumps({'ok': False, 'error': 'Nothing playing to restart'}), ctype='application/json')
                return
            
            if u.path == '/api/playback/status':
                with state['lock']:
                    proc = state['proc']
                    now = state['now']
                running = bool(proc and proc.poll() is None)
                with auto_restart_lock:
                    is_armed = auto_restart_armed
                self._send(200, json.dumps({'ok': True, 'running': running, 'now': now, 'armed': is_armed}), ctype='application/json')
                return

            if u.path == '/play':
                f = q.get('file', [''])[0]
                if not f:
                    self._send(400, 'Missing file')
                    return
                start_playback(f)
                self._send(302, '', ctype='text/plain')
                self.send_header('Location', '/')
                return
            
            if u.path == '/stop':
                stop_playback()
                self._send(302, '', ctype='text/plain')
                self.send_header('Location', '/')
                return

            # List tracks
            try:
                files = list_wavs(200)
            except Exception as e:
                files = []
                with state['lock']:
                    state['last_err'] = str(e)

            # Build UI
            with state['lock']:
                last_err = state.get('last_err')
            
            # Check status
            result = subprocess.run(['pw-link', '-l'], capture_output=True, text=True, env=_pw_env())
            links_ok = 'trackbot_player:output_FL' in result.stdout and 'Jamulus Injector-bot:input left' in result.stdout
            status_msg = "Ready" if links_ok else "Waiting for PipeWire..."

            rows = ''.join(f'<li><a href="/play?file={html.escape(f)}">{html.escape(f)}</a></li>' for f in files[:100])
            err_html = f'<p class="err">{html.escape(str(last_err))}</p>' if last_err else ''

            body = f'''<!doctype html>
<html><head><meta charset="utf-8"><title>TrackBot</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }}
h1 {{ font-size: 1.5rem; }}
ul {{ line-height: 1.6; max-height: 60vh; overflow: auto; }}
li {{ margin: .25rem 0; }}
.err {{ color: #c00; }}
.status {{ padding: .5rem; background: #f0f0f0; border-radius: 4px; margin: 1rem 0; }}
.controls {{ margin: 1rem 0; padding: 1rem; background: #f8f8f8; border-radius: 8px; }}
button {{ padding: 0.5rem 1rem; margin: 0.25rem; font-size: 1rem; cursor: pointer; }}
.armed {{ background: #ff9800; color: white; font-weight: bold; }}
.disarmed {{ background: #4caf50; color: white; }}
.warning {{ background: #fff3cd; padding: 0.5rem; border-radius: 4px; margin: 0.5rem 0; }}
</style></head>
<body>
<h1>🎵 TrackBot</h1>
<div class="status">Status: {html.escape(status_msg)}</div>

<div class="controls">
  <h3>Playback Control</h3>
  <button onclick="fetch('/stop').then(()=>location.reload())">⏹ Stop</button>
  <button onclick="fetch('/api/restart').then(()=>location.reload())">🔄 Restart Track</button>
  
  <h3>Auto-Restart</h3>
  <div id="arm-status" class="warning">
    Checking status...
  </div>
  <button id="arm-btn" onclick="toggleArm()" class="disarmed">Arm Auto-Restart</button>
  <p><small>Auto-arms 6 seconds after track starts. Track will restart after 6 seconds of silence. Click button to arm/disarm manually.</small></p>
</div>

<h3>Tracks</h3>
<ol>{rows}</ol>
{err_html}

<script>
async function updateStatus() {{
  try {{
    const resp = await fetch('/api/playback/status');
    const data = await resp.json();
    const armStatus = document.getElementById('arm-status');
    const armBtn = document.getElementById('arm-btn');
    if (data.armed) {{
      armStatus.innerHTML = '🔴 <b>ARMED</b> - Will restart after silence';
      armStatus.className = 'warning';
      armStatus.style.background = '#ffebee';
      armBtn.innerText = 'Disarm Auto-Restart';
      armBtn.className = 'armed';
    }} else {{
      armStatus.innerHTML = '🟢 <b>DISARMED</b> - Click to arm after singing starts';
      armStatus.className = 'warning';
      armStatus.style.background = '#e8f5e9';
      armBtn.innerText = 'Arm Auto-Restart';
      armBtn.className = 'disarmed';
    }}
  }} catch (e) {{
    console.error('Status check failed:', e);
  }}
}}

async function toggleArm() {{
  const btn = document.getElementById('arm-btn');
  const isArmed = btn.className === 'armed';
  try {{
    if (isArmed) {{
      await fetch('/api/autorestart/disarm');
    }} else {{
      await fetch('/api/autorestart/arm');
    }}
    updateStatus();
  }} catch (e) {{
    alert('Failed to toggle: ' + e);
  }}
}}

// Update status every 2 seconds
setInterval(updateStatus, 2000);
updateStatus();
</script>

</body></html>'''
            self._send(200, body)
        except Exception as e:
            import traceback
            err = traceback.format_exc()
            self._send(500, f'<pre>{html.escape(err)}</pre>')

if __name__ == '__main__':
    print(f"Starting TrackBot on http://{HOST}:{PORT}")
    start_silence_monitor()
    HTTPServer((HOST, PORT), Handler).serve_forever()
