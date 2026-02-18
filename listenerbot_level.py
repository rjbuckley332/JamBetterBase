#!/usr/bin/env python3
"""
ListenerBot - Level Monitor
Minimal UDP client that receives CLM_CHANNEL_LEVEL_LIST packets
Tracks per-client levels and ignores Injector-bot
"""

import socket
import struct
import time
import threading
import urllib.request
import json
import sys
import os

# Config
JAMULUS_SERVER = "127.0.0.1"
JAMULUS_PORT = 22124
TRACKBOT_URL = "http://127.0.0.1:8088/api/restart"
LOCAL_PORT = 0  # Auto-assign

# Silence detection  
SINGING_THRESHOLD = 4      # 4-bit level (0-15), above this = singing
SILENCE_DURATION = 6.0     # Seconds
CHECK_INTERVAL = 0.5       # Sample interval

# Client tracking
INJECTOR_BOT_NAME = "Injector-bot"
injector_bot_id = None     # Will be detected
client_names = {}          # id -> name

# State
singing_detected = False
silence_start = None
last_restart = 0

# Protocol
PROTMESSID_CLM_CHANNEL_LEVEL_LIST = 1015
PROTMESSID_CLM_CONN_CLIENTS_LIST = 1013
PROTMESSID_CLM_PING = 1001
PROTMESSID_VERSION_AND_OS = 29
PROTMESSID_CLM_VERSION_AND_OS = 1011

def crc16(data):
    """Jamulus CRC16."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return (~crc) & 0xFFFF

def parse_message(data):
    """Parse Jamulus message."""
    if len(data) < 2:
        return None
    
    tag = struct.unpack('<H', data[:2])[0]
    
    if tag == 0x0000:  # Regular message
        if len(data) < 7:
            return None
        msg_id, cnt, length = struct.unpack('<HBH', data[2:7])
        if len(data) < 9 + length:
            return None
        return {'type': 'regular', 'id': msg_id, 'cnt': cnt, 
                'data': data[7:7+length]}
    else:  # Connectionless
        return {'type': 'clm', 'id': tag, 'data': data[2:]}

def parse_client_list(data):
    """Parse CLM_CONN_CLIENTS_LIST to find Injector-bot."""
    global injector_bot_id, client_names
    
    pos = 0
    while pos + 10 < len(data):
        try:
            cid = data[pos]
            country = struct.unpack('<H', data[pos+1:pos+3])[0]
            instrument = struct.unpack('<I', data[pos+3:pos+7])[0]
            skill = data[pos+7]
            # Skip IP (4 bytes)
            name_len = struct.unpack('<H', data[pos+12:pos+14])[0]
            name = data[pos+14:pos+14+name_len].decode('utf-8', errors='ignore')
            
            client_names[cid] = name
            print(f"[ClientList] ID {cid}: {name}")
            
            if INJECTOR_BOT_NAME in name:
                injector_bot_id = cid
                print(f"[Detector] Found Injector-bot at ID {cid}")
            
            pos += 14 + name_len
            # Skip city
            if pos + 2 <= len(data):
                city_len = struct.unpack('<H', data[pos:pos+2])[0]
                pos += 2 + city_len
        except:
            break

def parse_channel_levels(data):
    """Parse CLM_CHANNEL_LEVEL_LIST."""
    levels = {}
    for i, byte in enumerate(data):
        levels[2*i] = byte & 0x0F
        levels[2*i + 1] = (byte >> 4) & 0x0F
    return levels

def check_singers(levels):
    """Check if any singer (not Injector-bot) is above threshold."""
    global singing_detected, silence_start
    
    anyone_singing = False
    status = []
    
    for cid, level in levels.items():
        name = client_names.get(cid, f"C{cid}")
        
        # Skip Injector-bot
        if cid == injector_bot_id:
            status.append(f"{name}:{level}(IGN)")
            continue
            
        is_singing = level > SINGING_THRESHOLD
        if is_singing:
            anyone_singing = True
        status.append(f"{name}:{level}{'*' if is_singing else ''}")
    
    print(f"[Levels] {' | '.join(status)}")
    
    return anyone_singing

def trigger_restart():
    """Call TrackBot restart API."""
    global last_restart
    
    if time.time() - last_restart < 10:
        return False
    
    try:
        req = urllib.request.Request(TRACKBOT_URL, method='POST', 
                                     data=b'', headers={'Content-Length': '0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode())
            if result.get('ok'):
                print("[RESTART] >>> Track restarted! <<<")
                last_restart = time.time()
                return True
    except Exception as e:
        print(f"[Error] {e}")
    return False

def monitor_thread(sock):
    """Receive and process level packets."""
    global singing_detected, silence_start
    
    while True:
        try:
            data, addr = sock.recvfrom(1500)
            msg = parse_message(data)
            
            if not msg:
                continue
            
            # Handle client list (find Injector-bot)
            if msg['id'] == PROTMESSID_CLM_CONN_CLIENTS_LIST:
                print("[RX] Client list received")
                parse_client_list(msg['data'])
            
            # Handle level list (main logic)
            elif msg['id'] == PROTMESSID_CLM_CHANNEL_LEVEL_LIST:
                levels = parse_channel_levels(msg['data'])
                
                if injector_bot_id is None:
                    print("[Warn] Injector-bot ID unknown yet")
                    continue
                
                anyone_singing = check_singers(levels)
                
                if anyone_singing:
                    if not singing_detected:
                        print("[State] Singing detected!")
                        singing_detected = True
                        silence_start = None
                else:
                    if singing_detected:
                        if silence_start is None:
                            print("[State] Silence started...")
                            silence_start = time.time()
                        else:
                            elapsed = time.time() - silence_start
                            if elapsed >= SILENCE_DURATION:
                                print(f"[State] Silence for {elapsed:.1f}s")
                                if trigger_restart():
                                    singing_detected = False
                                    silence_start = None
                    else:
                        # No singing yet, waiting...
                        pass
                        
        except Exception as e:
            print(f"[Error] {e}")

def send_ping(sock):
    """Send ping to keep connection alive."""
    # Simple ping
    msg = struct.pack('<H', PROTMESSID_CLM_PING)
    sock.sendto(msg, (JAMULUS_SERVER, JAMULUS_PORT))

def send_version(sock):
    """Send version info to register with server."""
    # CLM version message: version + OS
    version = b'\x03\x10\x00\x00'  # Jamulus 3.16.0
    os_name = b'Linux\x00'
    data = version + os_name
    msg = struct.pack('<H', PROTMESSID_CLM_VERSION_AND_OS) + data
    sock.sendto(msg, (JAMULUS_SERVER, JAMULUS_PORT))
    print("[TX] Version sent")

def main():
    print("=" * 50)
    print("ListenerBot - Per-Client Level Monitor")
    print("=" * 50)
    print(f"Server: {JAMULUS_SERVER}:{JAMULUS_PORT}")
    print(f"Singing threshold: {SINGING_THRESHOLD}/15")
    print(f"Silence duration: {SILENCE_DURATION}s")
    print()
    
    # Create socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', LOCAL_PORT))
    local_addr = sock.getsockname()
    print(f"[Socket] Bound to {local_addr}")
    
    # Send initial registration
    send_version(sock)
    time.sleep(0.5)
    
    # Start receiver thread
    receiver = threading.Thread(target=monitor_thread, args=(sock,))
    receiver.daemon = True
    receiver.start()
    
    # Keep alive
    while True:
        time.sleep(5)
        send_ping(sock)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Exit] Bye!")
        sys.exit(0)
