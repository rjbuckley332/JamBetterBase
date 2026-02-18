#!/usr/bin/env python3
"""
ListenerBot Silence Detector
Captures audio from ListenerBot JACK ports and detects silence.
Calls TrackBot API to restart track when singers are quiet.
"""

import os
import subprocess
import threading
import time
import struct
import math
import http.client
import tempfile

# Configuration
JACK_CLIENT_NAME = "ListenerBot"
TRACKBOT_HOST = "127.0.0.1"
TRACKBOT_PORT = 8088
SILENCE_THRESHOLD_DB = -60
SILENCE_DURATION_SECONDS = 6
CHECK_INTERVAL = 0.5
RECORD_DURATION = 0.5  # seconds per sample

# State machine
STATE_ARMED = "armed"      # Waiting for first singing
STATE_SINGING = "singing"  # Someone is singing
STATE_SILENCE = "silence"  # In silent period (counting down)

state = STATE_ARMED
silence_start_time = None
stop_event = threading.Event()

def _jack_env():
    env = os.environ.copy()
    env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    return env

def get_listenerbot_ports():
    """Find ListenerBot JACK output ports."""
    result = subprocess.run(
        ['jack_lsp'],
        capture_output=True, text=True, env=_jack_env()
    )
    if result.returncode != 0:
        return None, None
    
    left_port = None
    right_port = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if 'ListenerBot:out' in line or 'ListenerBot:output' in line:
            if 'left' in line.lower() or '_1' in line or 'FL' in line:
                left_port = line
            elif 'right' in line.lower() or '_2' in line or 'FR' in line:
                right_port = line
    
    return left_port, right_port

def calculate_rms_db_from_file(filepath):
    """Calculate RMS dB from WAV file."""
    try:
        with open(filepath, 'rb') as f:
            # Skip WAV header (44 bytes)
            f.seek(44)
            data = f.read()
        
        if len(data) < 2:
            return -100.0
        
        # Convert bytes to 16-bit samples
        count = len(data) // 2
        if count == 0:
            return -100.0
        
        # Sum of squares
        sum_squares = 0
        for i in range(0, len(data) - 1, 2):
            sample = struct.unpack('<h', data[i:i+2])[0]
            sum_squares += sample * sample
        
        rms = math.sqrt(sum_squares / count)
        if rms <= 0:
            return -100.0
        
        # Convert to dB (16-bit full scale = 32768)
        db = 20 * math.log10(rms / 32768.0)
        return db
    except Exception as e:
        print(f"Error calculating RMS: {e}")
        return -100.0

def restart_track():
    """Call TrackBot API to restart track."""
    try:
        conn = http.client.HTTPConnection(TRACKBOT_HOST, TRACKBOT_PORT, timeout=5)
        conn.request("GET", "/api/restart")
        response = conn.getresponse()
        data = response.read()
        conn.close()
        success = response.status == 200
        if success:
            print(f"[ListenerBot] Restart API response: {data.decode()[:100]}")
        return success
    except Exception as e:
        print(f"[ListenerBot] Failed to restart track: {e}")
        return False

def record_and_monitor():
    """Record from ListenerBot and monitor for silence."""
    global state, silence_start_time
    
    print("=" * 60)
    print("ListenerBot Silence Detector")
    print(f"Threshold: {SILENCE_THRESHOLD_DB} dB")
    print(f"Silence duration: {SILENCE_DURATION_SECONDS} seconds")
    print(f"TrackBot: http://{TRACKBOT_HOST}:{TRACKBOT_PORT}")
    print("=" * 60)
    print("ListenerBot: Waiting for JACK ports...")
    
    # Wait for ports to appear
    left_port = None
    right_port = None
    for _ in range(60):  # 60 seconds timeout
        left_port, right_port = get_listenerbot_ports()
        if left_port:
            break
        time.sleep(1)
    
    if not left_port:
        print(f"ListenerBot: Could not find {JACK_CLIENT_NAME} ports")
        return
    
    print(f"ListenerBot: Found port: {left_port}")
    print(f"ListenerBot: State: {state}")
    print("[ListenerBot] Monitoring for silence...")
    
    # Create temp file for recording
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
        tmp_path = tmp.name
    
    try:
        while not stop_event.is_set():
            try:
                # Remove old file
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                
                # Record short sample using jack_capture
                # jack_capture writes to file, auto-connects to first available port
                cmd = [
                    'jack_capture',
                    '-d', str(RECORD_DURATION),  # Duration in seconds
                    '-f', 'wav',                  # WAV format
                    '--port', left_port,          # Connect to this port
                    tmp_path
                ]
                
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=RECORD_DURATION + 5,
                    env=_jack_env()
                )
                
                if proc.returncode != 0 or not os.path.exists(tmp_path):
                    time.sleep(0.5)
                    continue
                
                # Calculate level
                db = calculate_rms_db_from_file(tmp_path)
                
                # State machine
                if state == STATE_ARMED:
                    if db > SILENCE_THRESHOLD_DB:
                        print(f"[ListenerBot] Singing detected: {db:.1f} dB -> STATE_SINGING")
                        state = STATE_SINGING
                        silence_start_time = None
                        
                elif state == STATE_SINGING:
                    if db <= SILENCE_THRESHOLD_DB:
                        print(f"[ListenerBot] Silence started: {db:.1f} dB -> STATE_SILENCE")
                        state = STATE_SILENCE
                        silence_start_time = time.time()
                        
                elif state == STATE_SILENCE:
                    if db > SILENCE_THRESHOLD_DB:
                        # Singing resumed
                        print(f"[ListenerBot] Singing resumed: {db:.1f} dB -> STATE_SINGING")
                        state = STATE_SINGING
                        silence_start_time = None
                    else:
                        # Still silent - check duration
                        elapsed = time.time() - silence_start_time
                        if elapsed >= SILENCE_DURATION_SECONDS:
                            print(f"[ListenerBot] Silence for {elapsed:.1f}s - RESTARTING")
                            if restart_track():
                                print("[ListenerBot] Track restarted successfully")
                            state = STATE_ARMED
                            silence_start_time = None
                            # Cooldown before detecting again
                            time.sleep(3)
                
            except subprocess.TimeoutExpired:
                continue
            except Exception as e:
                print(f"[ListenerBot] Error: {e}")
                time.sleep(0.5)
    finally:
        # Cleanup
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

def main():
    try:
        record_and_monitor()
    except KeyboardInterrupt:
        print("\nListenerBot: Stopping...")
        stop_event.set()

if __name__ == "__main__":
    main()
