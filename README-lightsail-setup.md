# Lightsail Jamulus Server - Self-Documentation
# Created: 2026-02-17
# Purpose: Complete standalone reference if nds-jamulus is unavailable

## Server Info
- IP: 13.221.152.126
- Host: ip-172-26-12-38 (AWS Lightsail)
- Domain: pipedreamers.reachhigher.ai (via Caddy reverse proxy)

## Services (systemd)
All services are enabled and start on boot:

1. jamulus-headless.service - Main Jamulus server
   - Records to: /var/lib/jamulus/recordings/
   
2. jamulus-injector.service - TrackBot backing track injector
   - Connects to: 127.0.0.1:22100 (JSON-RPC)
   - Web UI: http://127.0.0.1:8088
   
3. jamulus-toggle-webapp.service - Web control panel
   - Binds: 127.0.0.1:5000
   - Template: /home/nds/templates/updated_toggle_app_script.html
   - Served via Caddy to: pipedreamers.reachhigher.ai
   
4. jamulus-uploader.service - S3 upload daemon
   - Watches: /var/lib/jamulus/recordings/
   - Uploads to: s3://pipedreamers-recordings-prod/vps/vps-0001/
   - Script: /home/nds/jamulus_uploader.py

## Key Files
- /home/nds/.env - Environment variables
- /home/nds/toggle_app.py - Flask web app
- /home/nds/jamulus_uploader.py - S3 uploader
- /home/nds/Injector-bot.ini - TrackBot config
- /home/nds/recording_name_map.csv - Session name mappings
- /home/nds/saved_session_names.csv - Saved session names
- /home/nds/templates/updated_toggle_app_script.html - Web UI template

## File Locations
- Recordings: /var/lib/jamulus/recordings/
- Upload markers: /var/lib/jamulus/recordings/.uploaded/
- JSON-RPC secret: /var/lib/jamulus/jsonrpc-secret.txt
- Caddy config: /etc/caddy/Caddyfile

## Useful Commands
```bash
# Check all Jamulus services
sudo systemctl status jamulus-headless jamulus-injector jamulus-toggle-webapp jamulus-uploader

# View logs
sudo journalctl -u jamulus-uploader -f
sudo journalctl -u jamulus-toggle-webapp -f

# Restart web UI after template changes
sudo systemctl restart jamulus-toggle-webapp

# Check S3 upload status
ls -la /var/lib/jamulus/recordings/.uploaded/

# Test S3 access
aws s3 ls s3://pipedreamers-recordings-prod/vps/vps-0001/recordings/
```

## Web UI Tabs (as of 2026-02-17)
- Control: Start/stop recording, metronome, saved sessions
- Backing Track: Browse S3, select backing tracks, temp upload

## S3 Structure
```
s3://pipedreamers-recordings-prod/vps/vps-0001/
├── recordings/
│   └── YYYY-MM-DD/
│       └── SessionName/
│           ├── XXXXXX_YYMMDD_PART.wav
│           └── XXXXXX_YYMMDD_MIXL.mp3
└── tracks/
    └── tmp/  (24h temp uploads)
```

## Dependencies
- Python 3.12 + Flask (toggle webapp)
- Gunicorn (WSGI server)
- AWS CLI v2 (S3 operations)
- boto3 (Python AWS SDK)
- ffmpeg (audio processing)
- Caddy (reverse proxy + TLS)
