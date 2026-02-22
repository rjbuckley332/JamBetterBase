#!/usr/bin/env python3
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
from pathlib import Path
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
metronome_volume = 0.8
metronome_sig = "4/4"
metronome_seq = 0  # incrementing token to cancel stale async linkers

# Debounce metronome updates so sliders can feel realtime without thrashing
# the audio process on every tiny movement.
metronome_pending = {'bpm': None, 'vol': None, 'sig': None}
metronome_debounce_thread = None
metronome_debounce_evt = threading.Event()
metronome_debounce_delay = 0.20  # seconds

# Auto-restart on silence state
auto_restart_lock = threading.Lock()
auto_restart_config = {"enabled": False, "silence_seconds": 6, "threshold_db": -60}
auto_restart_thread = None
auto_restart_stop = threading.Event()
last_restart_time = 0

def run(cmd, stdin=None, check=True):
    p = subprocess.run(cmd, input=stdin, text=False if stdin is not None else True,
                       capture_output=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {cmd}\n{p.stderr.decode() if isinstance(p.stderr, (bytes, bytearray)) else p.stderr}")
    return p

def list_wavs(limit=200):
    # Only files, wav/WAV
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
    """Start playback into Jamulus via JACK.

    Uses rclone to fetch the file under RCLONE_REMOTE, decodes (if needed) to WAV,
    then plays via jack-play (jack-tools)."""
    stop_playback()

    full = f"{RCLONE_REMOTE.rstrip('/')}/{relpath.lstrip('/')}"

    import hashlib, tempfile, time
    from pathlib import Path

    ext = (relpath.rsplit('.', 1)[-1].lower() if '.' in relpath else 'wav')
    h = hashlib.sha1(full.encode('utf-8')).hexdigest()[:10]
    tmpdir = Path(tempfile.gettempdir()) / 'trackbot'
    tmpdir.mkdir(parents=True, exist_ok=True)
    src_path = tmpdir / f"src_{h}.{ext}"
    wav_path = tmpdir / f"play_{h}.wav"

    # Check if we already have the converted WAV file cached locally
    if wav_path.exists():
        # Use cached file - skip download and conversion
        pass
    else:
        # Fetch source to local file (streaming; avoids holding in memory)
        rclone_cmd = ['rclone','cat',full] + RCLONE_FLAGS
        with open(src_path, 'wb') as f:
            r = subprocess.run(rclone_cmd, stdout=f, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or b'').decode('utf-8', errors='replace') or 'rclone cat failed')

        # Decode/normalize to 48k WAV stereo (Jamulus-friendly, easy L/R routing)
        ffmpeg_cmd = ['ffmpeg','-hide_banner','-loglevel','error','-y','-i', str(src_path), '-ac','2','-ar','48000', str(wav_path)]
        d = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
        if d.returncode != 0:
            raise RuntimeError(d.stderr.strip() or 'ffmpeg decode failed')

    # Play via PipeWire (pw-cat) and explicitly link to the injector inputs.
    # This avoids JACK timing glitches we observed with gst jackaudiosink on this VPS.
    # We set node.name=trackbot_player so we can find/link the ports deterministically.
    play_cmd = [
        'pw-cat', '-p',
        '--target', '0',
        '--latency', '200ms',
        '--properties', 'node.name=trackbot_player',
        '--properties', 'media.role=Music',
        str(wav_path)
    ]
    proc = subprocess.Popen(play_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=_pw_env())

    # Wait briefly for PipeWire ports to appear, then link to injector inputs
    out_l = None
    out_r = None
    for _ in range(60):
        out = subprocess.run(['pw-link', '-o'], capture_output=True, text=True, env=_pw_env())
        if out.returncode == 0:
            for ln in out.stdout.splitlines():
                ln = ln.strip()
                # pw-cat ports are typically named like: trackbot_player:output_FL / output_FR
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
    else:
        with state['lock']:
            state['last_err'] = 'pw-cat started but no PipeWire ports found (trackbot_player:output_FL/FR)'

    with state['lock']:
        state['proc'] = proc
        state['now'] = relpath



def _parse_time_signature(sig: str) -> tuple[int, int]:
    s = (sig or '4/4').strip()
    try:
        a, b = s.split('/', 1)
        n = int(a); d = int(b)
    except Exception:
        return (4, 4)
    if (n, d) not in {(2,4),(3,4),(4,4),(6,8),(12,8)}:
        return (4, 4)
    return (n, d)



def _ensure_click_samples(sr: int = 48000) -> tuple[str, str]:
    """Return static strong/weak click samples (repo assets) and transcode if needed."""
    base = Path('/home/nds/trackbot/samples')
    src_strong = base / 'click_strong.wav'
    src_weak = base / 'click_weak.wav'

    # Runtime copies normalized to target sample-rate
    strong = f"/tmp/trackbot_click_asset_strong_{sr}.wav"
    weak = f"/tmp/trackbot_click_asset_weak_{sr}.wav"

    if src_strong.exists() and (not os.path.exists(strong) or os.path.getsize(strong) < 1024):
        subprocess.run([
            'ffmpeg', '-nostdin', '-y', '-i', str(src_strong),
            '-ac', '1', '-ar', str(sr), '-c:a', 'pcm_s16le', strong
        ], capture_output=True, text=True)

    if src_weak.exists() and (not os.path.exists(weak) or os.path.getsize(weak) < 1024):
        subprocess.run([
            'ffmpeg', '-nostdin', '-y', '-i', str(src_weak),
            '-ac', '1', '-ar', str(sr), '-c:a', 'pcm_s16le', weak
        ], capture_output=True, text=True)

    return strong, weak


def _read_wav_mono_pcm16(path: str, sr: int) -> bytes:
    with wave.open(path, 'rb') as w:
        ch = w.getnchannels()
        sw = w.getsampwidth()
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    if sw != 2:
        return b''
    if rate != sr:
        return b''
    if ch == 1:
        return frames
    # downmix basic stereo -> mono (take L)
    out = bytearray()
    for i in range(0, len(frames), ch * 2):
        out += frames[i:i+2]
    return bytes(out)


def _generate_click_wav(wav_path: str, bpm: int, seconds: int = 8, sr: int = 48000, volume: float = 0.25, signature: str = "4/4"):
    """Sample-based metronome rendering using ffmpeg-generated click assets."""
    num, den = _parse_time_signature(signature)

    quarter_sec = 60.0 / max(1, bpm)
    unit_sec = quarter_sec if den == 4 else (quarter_sec / 2.0)
    unit_samples = max(1, int(round(unit_sec * sr)))

    if (num, den) == (2, 4):
        pattern = ['S', 'W']
    elif (num, den) == (3, 4):
        pattern = ['S', 'W', 'W']
    elif (num, den) == (4, 4):
        pattern = ['S', 'W', 'W', 'W']
    elif (num, den) == (6, 8):
        pattern = ['S', 'W', 'W', 'S', 'W', 'W']
    elif (num, den) == (12, 8):
        pattern = ['S', 'W', 'W', 'S', 'W', 'W', 'S', 'W', 'W', 'S', 'W', 'W']
    else:
        pattern = ['S', 'W', 'W', 'W']

    strong_path, weak_path = _ensure_click_samples(sr)
    strong = _read_wav_mono_pcm16(strong_path, sr)
    weak = _read_wav_mono_pcm16(weak_path, sr)
    if not strong or not weak:
        # fail-safe silence file
        strong = b'\x00\x00' * min(2400, unit_samples)
        weak = strong

    # apply requested volume at render-level too (kept conservative)
    gain = max(0.0, min(1.0, float(volume)))
    def scale_pcm16(raw: bytes, factor: float) -> bytes:
        out = bytearray()
        f = max(0.0, min(1.0, factor))
        for i in range(0, len(raw), 2):
            v = int.from_bytes(raw[i:i+2], 'little', signed=True)
            vv = int(max(-32768, min(32767, int(v * f))))
            out += int(vv).to_bytes(2, 'little', signed=True)
        return bytes(out)

    strong = scale_pcm16(strong, gain)
    weak = scale_pcm16(weak, gain)

    click_len = min(len(strong)//2, len(weak)//2, unit_samples)
    strong = strong[:click_len*2]
    weak = weak[:click_len*2]
    silence = b'\x00\x00' * (unit_samples - click_len)

    bar = bytearray()
    for sym in pattern:
        bar += (strong if sym == 'S' else weak)
        bar += silence

    total_samples = int(max(1, seconds) * sr)
    bar_samples = unit_samples * len(pattern)
    bars = max(1, (total_samples + bar_samples - 1) // bar_samples)
    buf = bytearray()
    for _ in range(bars):
        buf += bar
    buf = buf[:total_samples * 2]

    with wave.open(wav_path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(buf))



def _pw_env():
    env = os.environ.copy()
    # systemd user services sometimes miss XDG_RUNTIME_DIR when lingered
    env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    return env


def _connect_port_to_jamulus(port: str):
    # Ensure Jamulus injector inputs are not accidentally fed from dummy capture ports.
    try:
        subprocess.run(['pw-jack','jack_disconnect', 'system:capture_1', JAMULUS_IN_L], capture_output=True, text=True, env=_pw_env())
        subprocess.run(['pw-jack','jack_disconnect', 'system:capture_2', JAMULUS_IN_R], capture_output=True, text=True, env=_pw_env())
    except Exception:
        pass
    subprocess.run(['pw-jack','jack_connect', port, JAMULUS_IN_L], capture_output=True, text=True, env=_pw_env())
    subprocess.run(['pw-jack','jack_connect', port, JAMULUS_IN_R], capture_output=True, text=True, env=_pw_env())



def _apply_metronome_now(bpm: int, volume: float, signature: str = "4/4"):
    """Apply metronome change immediately (restarts the audio process)."""
    global metronome_proc, metronome_bpm, metronome_volume, metronome_sig, metronome_seq

    bpm = int(bpm)
    if bpm < 40: bpm = 40
    if bpm > 240: bpm = 240
    vol = float(volume)
    if vol < 0: vol = 0.0
    if vol > 1.5: vol = 1.5
    sig = (signature or "4/4").strip()

    with metronome_lock:
        metronome_seq += 1
        my_seq = metronome_seq
        metronome_volume = vol
        metronome_sig = sig

    # Stop existing instance before starting a new one
    stop_metronome()

    # Generating a huge WAV on every slider move can block for tens of seconds.
    # Cache a shorter file per BPM and only (re)generate when missing.
    sig_key = sig.replace("/", "-")
    wav_path = f"/tmp/trackbot_metro_v6_{bpm}_{sig_key}.wav"
    try:
        if not os.path.exists(wav_path) or os.path.getsize(wav_path) < 4096:
            _generate_click_wav(wav_path, bpm=bpm, seconds=120, sr=48000, volume=1.0, signature=sig)
    except Exception:
        # If generation fails for any reason, fall back to a small file
        _generate_click_wav(wav_path, bpm=bpm, seconds=15, sr=48000, volume=1.0, signature=sig)

    cmd = [
        'pw-cat', '-p',
        '--target', '0',
        '--latency', '200ms',
        '--properties', 'node.name=trackbot_metro',
        '--properties', 'media.role=Metronome',
        '--volume', f'{vol}',
        wav_path,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=_pw_env())

    with metronome_lock:
        if my_seq != metronome_seq:
            try: proc.terminate()
            except Exception: pass
            return
        metronome_proc = proc
        metronome_bpm = bpm

    # Link ports asynchronously so start feels immediate; retry for a few seconds.
    def _link_async(seq_token: int):
        deadline = time.time() + 6.0
        out_l = out_r = out_mono = None
        while time.time() < deadline:
            with metronome_lock:
                if seq_token != metronome_seq:
                    return
            outp = subprocess.run(['pw-link', '-o'], capture_output=True, text=True, env=_pw_env())
            if outp.returncode == 0:
                for ln in outp.stdout.splitlines():
                    ln = ln.strip()
                    if ln == 'trackbot_metro:output_FL': out_l = ln
                    elif ln == 'trackbot_metro:output_FR': out_r = ln
                    elif ln == 'trackbot_metro:output_MONO': out_mono = ln
            if out_l and out_r:
                subprocess.run(['pw-link', out_l, 'Jamulus Injector-bot:input left'], capture_output=True, text=True, env=_pw_env())
                subprocess.run(['pw-link', out_r, 'Jamulus Injector-bot:input right'], capture_output=True, text=True, env=_pw_env())
                return
            if out_mono:
                subprocess.run(['pw-link', out_mono, 'Jamulus Injector-bot:input left'], capture_output=True, text=True, env=_pw_env())
                subprocess.run(['pw-link', out_mono, 'Jamulus Injector-bot:input right'], capture_output=True, text=True, env=_pw_env())
                return
            time.sleep(0.1)
        with state['lock']:
            state['last_err'] = 'Metronome: PipeWire ports not found (trackbot_metro:output_*).'

    threading.Thread(target=_link_async, args=(my_seq,), daemon=True).start()

def _ensure_metronome_debouncer():
    global metronome_debounce_thread
    if metronome_debounce_thread and metronome_debounce_thread.is_alive():
        return

    def _loop():
        # Wait for updates, then apply after a quiet period.
        while True:
            metronome_debounce_evt.wait()
            metronome_debounce_evt.clear()
            # quiet period debounce
            while True:
                time.sleep(metronome_debounce_delay)
                if metronome_debounce_evt.is_set():
                    metronome_debounce_evt.clear()
                    continue
                break

            with metronome_lock:
                bpm = metronome_pending.get('bpm')
                vol = metronome_pending.get('vol')
                sig = metronome_pending.get('sig')

            if bpm is None or vol is None:
                continue
            if sig is None:
                sig = "4/4"

            try:
                _apply_metronome_now(bpm, vol, sig)
            except Exception as e:
                with state['lock']:
                    state['last_err'] = f"Metronome update failed: {e}"

    metronome_debounce_thread = threading.Thread(target=_loop, daemon=True)
    metronome_debounce_thread.start()


def start_metronome(bpm: int, volume: float = 0.8, signature: str = "4/4"):
    """Start/update metronome immediately.

    The web UI already debounces slider events, so we don't need extra
    debounce logic here.
    """
    _apply_metronome_now(bpm, volume, signature)

def stop_metronome():
    """Stop metronome precisely: kill pw-cat and unlink its ports."""
    global metronome_proc, metronome_bpm

    with metronome_lock:
        proc = metronome_proc
        metronome_proc = None
        metronome_bpm = None

    # Unlink any existing metro links (ignore errors).
    try:
        subprocess.run(['pw-link', '-d', 'trackbot_metro:output_FL', 'Jamulus Injector-bot:input left'],
                       capture_output=True, text=True, env=_pw_env())
        subprocess.run(['pw-link', '-d', 'trackbot_metro:output_FR', 'Jamulus Injector-bot:input right'],
                       capture_output=True, text=True, env=_pw_env())
        subprocess.run(['pw-link', '-d', 'trackbot_metro:output_MONO', 'Jamulus Injector-bot:input left'],
                       capture_output=True, text=True, env=_pw_env())
        subprocess.run(['pw-link', '-d', 'trackbot_metro:output_MONO', 'Jamulus Injector-bot:input right'],
                       capture_output=True, text=True, env=_pw_env())
    except Exception:
        pass

    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # Hard cleanup any orphan metronome playback processes.
    try:
        subprocess.run(['pkill', '-f', 'pw-cat -p .*node.name=trackbot_metro'], capture_output=True, text=True, env=_pw_env())
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):

    def _send(self, code, body, ctype='text/html; charset=utf-8'):
        body_b = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body_b)))
        self.end_headers()
        self.wfile.write(body_b)

    def do_HEAD(self):
        # Simple health check; mirrors GET without body
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
            if u.path == '/api/jamulus/restart':
                try:
                    # Gentle + async: stop any playback, then restart Jamulus client service.
                    # Do not block HTTP response on GUI startup.
                    stop_playback()
                    cmd = ['systemctl','--user','restart','jamulus-client.service']
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self._send(200, json.dumps({'ok': True}), ctype='application/json; charset=utf-8')
                except Exception as e:
                    self._send(500, json.dumps({'ok': False, 'error': str(e)}), ctype='application/json; charset=utf-8')
                return
            if u.path == '/api/restart':
                # Restart current playback from beginning
                with state['lock']:
                    now = state['now']
                if now:
                    stop_playback()
                    time.sleep(0.1)
                    start_playback(now)
                    self._send(200, json.dumps({'ok': True, 'restarted': now}), ctype='application/json; charset=utf-8')
                else:
                    self._send(400, json.dumps({'ok': False, 'error': 'Nothing playing to restart'}), ctype='application/json; charset=utf-8')
                return
            if u.path == '/api/metronome/start':
                bpm = int(q.get('bpm', ['100'])[0] or 100)
                vol = float(q.get('vol', ['0.8'])[0] or 0.8)
                sig = (q.get('sig', ['4/4'])[0] or '4/4')
                start_metronome(bpm, vol, sig)
                self._send(200, json.dumps({'ok': True, 'bpm': bpm, 'vol': vol, 'sig': sig}), ctype='application/json; charset=utf-8')
                return
            if u.path == '/api/metronome/stop':
                stop_metronome()
                self._send(200, json.dumps({'ok': True}), ctype='application/json; charset=utf-8')
                return
            if u.path == '/api/metronome/status':
                proc = metronome_proc
                running = bool(proc and proc.poll() is None)
                with state['lock']:
                    last_err = state.get('last_err')
                self._send(200, json.dumps({'ok': True, 'running': running, 'bpm': metronome_bpm, 'vol': metronome_volume, 'sig': metronome_sig, 'last_err': last_err}), ctype='application/json; charset=utf-8')
                return
            if u.path == '/api/playback/status':
                with state['lock']:
                    proc = state['proc']
                    now = state['now']
                running = bool(proc and proc.poll() is None)
                if not running:
                    # Fallback: if a pw-cat metronome process exists, treat as running (fixes UI realtime sliders)
                    try:
                        running = (subprocess.run(["pgrep","-u","nds","-f","pw-cat -p .*node.name=trackbot_metro"], capture_output=True).returncode == 0)
                    except Exception:
                        pass
                self._send(200, json.dumps({'ok': True, 'running': running, 'now': now}), ctype='application/json; charset=utf-8')
                return
            if u.path == '/api/jamulus/hardreset':
                try:
                    stop_playback()
                    cmds = [
                        ['systemctl','--user','stop','jamulus-client.service'],
                        ['pkill','-f','/usr/bin/Jamulus'],
                        ['pkill','-x','Jamulus'],
                        ['sleep','2'],
                        ['systemctl','--user','start','jamulus-client.service'],
                    ]
                    for cmd in cmds:
                        r = subprocess.run(cmd, capture_output=True, text=True)
                        if cmd[0] == 'pkill' and r.returncode in (0,1):
                            continue
                        if r.returncode != 0:
                            err = (r.stderr or r.stdout or '').strip()
                            self._send(500, json.dumps({'ok': False, 'error': err, 'cmd': cmd}), ctype='application/json; charset=utf-8')
                            return
                    self._send(200, json.dumps({'ok': True}), ctype='application/json; charset=utf-8')
                except Exception as e:
                    self._send(500, json.dumps({'ok': False, 'error': str(e)}), ctype='application/json; charset=utf-8')
                return
            if u.path == '/api/list':
                # JSON directory listing within RCLONE_REMOTE root
                sub = (q.get('path',[''])[0] or '').lstrip('/')
                # normalize: allow '' or 'foo/bar/'
                base = RCLONE_REMOTE.rstrip('/')
                target = base + ('/' + sub if sub else '')
                try:
                    # dirs
                    cmd_dirs = ['rclone','lsf',target,'--dirs-only'] + RCLONE_FLAGS
                    pd = subprocess.run(cmd_dirs, text=True, capture_output=True)
                    if pd.returncode != 0:
                        raise RuntimeError(pd.stderr.strip() or 'rclone dirs failed')
                    dirs = sorted([d.strip().rstrip('/') for d in pd.stdout.splitlines() if d.strip()])
                    # wav files
                    cmd_files = ['rclone','lsf',target,'--files-only','--include','*.wav','--include','*.WAV','--include','*.mp3','--include','*.MP3'] + RCLONE_FLAGS
                    pf = subprocess.run(cmd_files, text=True, capture_output=True)
                    if pf.returncode != 0:
                        raise RuntimeError(pf.stderr.strip() or 'rclone files failed')
                    files = sorted([f.strip() for f in pf.stdout.splitlines() if f.strip()])
                    resp = {'ok': True, 'path': sub, 'dirs': dirs, 'files': files, 'root': RCLONE_REMOTE}
                    self._send(200, json.dumps(resp), ctype='application/json; charset=utf-8')
                except Exception as e:
                    resp = {'ok': False, 'path': sub, 'error': str(e), 'root': RCLONE_REMOTE}
                    self._send(500, json.dumps(resp), ctype='application/json; charset=utf-8')
                return
            if u.path == '/stop':
                stop_playback()
                self.send_response(302)
                self.send_header('Location','/')
                self.end_headers()
                return
            if u.path == '/play':
                f = q.get('file',[None])[0]
                if not f:
                    self._send(400,'missing file')
                    return
                start_playback(f)
                self.send_response(302)
                self.send_header('Location','/')
                self.end_headers()
                return
            if u.path == '/api/restart':
                with state['lock']:
                    current = state['now']
                if not current:
                    self._send(200, json.dumps({'ok': False, 'error': 'No track currently loaded'}), ctype='application/json; charset=utf-8')
                    return
                stop_playback()
                start_playback(current)
                self._send(200, json.dumps({'ok': True, 'restarted': current}), ctype='application/json; charset=utf-8')
                return

            # index
            with state['lock']:
                now = state['now']
                last_err = state['last_err']

            try:
                wavs = list_wavs()
            except Exception as e:
                wavs = []
                last_err = str(e)
                with state['lock']:
                    state['last_err'] = last_err

            rows = []
            for w in wavs:
                esc = html.escape(w)
                href = '/play?file=' + urllib.parse.quote(w)
                rows.append(f'<li><a href="{href}">{esc}</a></li>')
            now_html = html.escape(now) if now else '(nothing)'

            body = f"""<!doctype html>
<html><head><meta charset=utf-8><title>TrackBot</title>
<style>body{{font-family:system-ui,Arial,sans-serif;max-width:900px;margin:20px auto;padding:0 12px}} code{{background:#f4f4f4;padding:2px 4px;border-radius:4px}} .err{{color:#b00020}}</style>
</head><body>
<h1>TrackBot</h1>
<p><b>Remote:</b> <code>{html.escape(RCLONE_REMOTE)}</code></p>
<p><b>Now playing:</b> <code>{now_html}</code> &nbsp; <a href="/stop">Stop</a></p>
"""
            if last_err:
                body += f"<p class=err><b>Last error:</b> {html.escape(last_err)}</p>"
            body += "<h2>WAVs</h2><ol>" + "\n".join(rows) + "</ol>"
            body += "</body></html>"
            self._send(200, body)
        except Exception as e:
            self._send(500, f"error: {html.escape(str(e))}")

    def do_POST(self):
        try:
            u = urllib.parse.urlparse(self.path)
            if u.path == '/api/restart':
                # Restart current playback from beginning
                with state['lock']:
                    now = state['now']
                if now:
                    stop_playback()
                    time.sleep(0.1)
                    start_playback(now)
                    self._send(200, json.dumps({'ok': True, 'restarted': now}), ctype='application/json; charset=utf-8')
                else:
                    self._send(400, json.dumps({'ok': False, 'error': 'Nothing playing to restart'}), ctype='application/json; charset=utf-8')
                return
            self._send(404, json.dumps({'ok': False, 'error': 'Not found'}), ctype='application/json; charset=utf-8')
        except Exception as e:
            self._send(500, json.dumps({'ok': False, 'error': str(e)}), ctype='application/json; charset=utf-8')

    def log_message(self, fmt, *args):
        return


if __name__ == '__main__':
    print(f"TrackBot web running on http://{HOST}:{PORT}/")
    print(f"Using rclone remote: {RCLONE_REMOTE} (flags: {' '.join(RCLONE_FLAGS)})")
    print(f"Jamulus inputs: {JAMULUS_IN_L} / {JAMULUS_IN_R}")
    httpd = HTTPServer((HOST, PORT), Handler)
    httpd.serve_forever()
