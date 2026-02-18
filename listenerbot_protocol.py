#!/usr/bin/env python3
"""
ListenerBot - Minimal level listener
Receives CLM_CHANNEL_LEVEL_LIST packets only
"""

import socket
import struct
import time
import threading
import urllib.request
import json
import sys

# Configuration
JAMULUS_SERVER = "127.0.0.1"
JAMULUS_PORT = 22124
TRACKBOT_URL = "http://127.0.0.1:8088/api/restart"

# Silence detection
SILENCE_THRESHOLD = 2  # 4-bit level (0-15), 0-2 = silent
SILENCE_DURATION = 6.0
CHECK_INTERVAL = 0.1

# Protocol constants
PROT_TAG = 0x0000
PROTMESSID_CLM_REQ_CHANNEL_LEVEL_LIST = 1014
PROTMESSID_CLM_CHANNEL_LEVEL_LIST = 1015
PROTMESSID_VERSION_AND_OS = 29
PROTMESSID_CLM_VERSION_AND_OS = 1011

def crc16_jamulus(data):
    """Jamulus CRC16: x^16 + x^12 + x^5 + 1, initial 0xFFFF, transmitted inverted."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return (~crc) & 0xFFFF

def create_clm_message(msg_id, data=b''):
    """Create connectionless message."""
    # CLM frame: ID(2) + data(n)
    msg = struct.pack('<H', msg_id) + data
    return msg

def parse_message(data):
    """Parse Jamulus message."""
    if len(data) < 2:
        return None
    
    # Check if it's a regular message (starts with TAG)
    tag = struct.unpack('<H', data[:2])[0]
    
    if tag == PROT_TAG:
        # Regular message with connection
        if len(data) < 7:
            return None
        msg_id, cnt, length = struct.unpack('<HBH', data[2:7])
        if len(data) < 7 + length + 2:
            return None
        body = data[7:7+length]
        return {'type': 'regular', 'id': msg_id, 'cnt': cnt, 'data': body}
    else:
        # Connectionless message
        msg_id = tag
        body = data[2:]
        return {'type': 'clm', 'id': msg_id, 'data': body}

def parse_channel_levels(data):
    """Parse CLM_CHANNEL_LEVEL_LIST data."""
    # Format: 4 bits per client, packed 2 per byte
    # Lower nibble = even client, upper nibble = odd client
    levels = {}
    for i, byte in enumerate(data):
        levels[2*i] = byte & 0x0F
        if 2*i + 1 < 40:  # Support up to 40 clients
            levels[2*i + 1] = (byte >> 4) & 0x0F
    return levels

class LevelListener:
    def __init__(self):
        self.sock = None
        self.running = False
        self.silence_start = None
        self.last_restart = 0
        self.client_names = {0: 'PD-Rich-PI', 1: 'Injector-bot', 2: 'ListenerBot'}
        
    def trigger_restart(self):
        """Call TrackBot restart API."""
        current_time = time.time()
        if current_time - self.last_restart < 10:
            return
            
        try:
            req = urllib.request.Request(TRACKBOT_URL, method='POST')
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read().decode())
                if result.get('ok'):
                    print("[ListenerBot] >>> TRACK RESTARTED <<<")
                    self.last_restart = current_time
                else:
                    print(f"[ListenerBot] Restart failed: {result}")
        except Exception as e:
            print(f"[ListenerBot] Error: {e}")
    
    def check_silence(self, levels):
        """Check if all singers (except Injector-bot) are silent."""
        # Client 1 is Injector-bot - ignore it
        # Client 2 is ListenerBot itself - ignore it
        any_singing = False
        
        for cid, level in levels.items():
            if cid in [1, 2]:  # Ignore Injector-bot and ourselves
                continue
            if level > SILENCE_THRESHOLD:
                any_singing = True
                break
        
        return not any_singing
    
    def receive_loop(self):
        """Main receive loop."""
        print("[ListenerBot] Listening for level packets...")
        self.running = True
        
        while self.running:
            try:
                self.sock.settimeout(1.0)
                data, addr = self.sock.recvfrom(1500)
                
                msg = parse_message(data)
                if msg is None:
                    continue
                
                if msg['id'] == PROTMESSID_CLM_CHANNEL_LEVEL_LIST:
                    levels = parse_channel_levels(msg['data'])
                    
                    # Debug: show levels
                    level_str = ", ".join([f"{self.client_names.get(cid, f'C{cid}')}:{lvl}" 
                                          for cid, lvl in levels.items() if lvl > 0])
                    print(f"[Levels] {level_str}")
                    
                    # Check silence
                    if self.check_silence(levels):
                        if self.silence_start is None:
                            self.silence_start = time.time()
                            print("[ListenerBot] Silence detected, waiting...")
                        else:
                            elapsed = time.time() - self.silence_start
                            if elapsed >= SILENCE_DURATION:
                                print(f"[ListenerBot] Silence for {elapsed:.1f}s - RESTARTING")
                                self.trigger_restart()
                                self.silence_start = None
                    else:
                        if self.silence_start is not None:
                            print("[ListenerBot] Singing resumed")
                        self.silence_start = None
                        
            except socket.timeout:
                continue
            except Exception as e:
                print(f"[ListenerBot] Error: {e}")
    
    def request_levels(self):
        """Send level request to server."""
        # We need to send a message to the server to start receiving level updates
        # The server sends levels only to clients that request them
        
        # Try sending a version request first (this identifies us to the server)
        ver_msg = create_clm_message(PROTMESSID_CLM_VERSION_AND_OS, b'\x03\x10\x00\x00Linux')
        self.sock.sendto(ver_msg, (JAMULUS_SERVER, JAMULUS_PORT))
        print("[ListenerBot] Sent version info")
        
        # Send level request
        req_msg = create_clm_message(PROTMESSID_CLM_REQ_CHANNEL_LEVEL_LIST)
        self.sock.sendto(req_msg, (JAMULUS_SERVER, JAMULUS_PORT))
        print("[ListenerBot] Requested level updates")
    
    def run(self):
        """Main entry point."""
        print("=" * 50)
        print("ListenerBot - Minimal Level Listener")
        print("=" * 50)
        
        # Create UDP socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', 0))
        local_port = self.sock.getsockname()[1]
        print(f"[ListenerBot] Bound to UDP port {local_port}")
        
        # Send initial request
        self.request_levels()
        
        # Start receive thread
        self.receive_thread = threading.Thread(target=self.receive_loop)
        self.receive_thread.daemon = True
        self.receive_thread.start()
        
        # Keep sending requests periodically
        while True:
            time.sleep(5)
            self.request_levels()

if __name__ == '__main__':
    listener = LevelListener()
    try:
        listener.run()
    except KeyboardInterrupt:
        print("\n[ListenerBot] Stopping...")
        listener.running = False
        sys.exit(0)
