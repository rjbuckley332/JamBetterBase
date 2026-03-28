# PipeDreamers Jamulus Server

This repository contains the configuration and applications for the PipeDreamers Jamulus server hosted on AWS Lightsail.

## Overview

This server provides:
- **Jamulus audio server** for real-time jam sessions
- **Web control interface** (toggle_app.py) for managing the server
- **Track playback system** (trackbot) for playing backing tracks
- **Audio injection system** for injecting audio into Jamulus
- **Listener bot** for monitoring and recording sessions
- **File upload/management** for backing tracks and recordings

## Architecture

### Core Components

| Component | File | Purpose |
|-----------|------|---------|
| Web Control | `toggle_app.py` | Main Flask web app for server control |
| Unified Ops Dashboard | `ops_dashboard_app.py` | Fleet status + start/stop controls + operator pins |
| Track Player | `trackbot/app.py` | Playback system for backing tracks |
| UI Template | `templates/updated_toggle_app_script.html` | Web interface |
| Practice App | `pitch-monitor/` | Customer-facing pitch/practice web assets |
| Uploader | `jamulus_uploader.py` | S3 upload functionality |
| Level Monitor | `listenerbot_level.py` | Audio level monitoring |
| Protocol Bot | `listenerbot_protocol.py` | Session protocol/management |
| Silence Detector | `listenerbot_silence.py` | Silence detection for recording |


## Repo Role

This repository is the **canonical product/runtime repo** for JamBetter.

Anything that is deployed to customer-facing hosts or changes live app behavior belongs here, including:
- `toggle_app.py`
- `templates/updated_toggle_app_script.html`
- `trackbot/`
- `config/`, `deploy/`, `docs/`
- `pitch-monitor/` practice app assets

Operational notes, assistant memory, and OpenClaw-specific working files belong in `JamBetter-Claw`, not here.

### Systemd Services

All services are managed via systemd:
- `jackd.service` - JACK audio server
- `jamulus-headless.service` - Main Jamulus server
- `jamulus-toggle-webapp.service` - Web control interface
- `jamulus-injector.service` - Audio injection
- `jamulus-uploader.service` - S3 upload service
- `listenerbot.service` - Main listener bot
- `listenerbot-silence.service` - Silence detection bot
- `trackbot-web.service` - Track playback web interface

### Web Server

Caddy is used as the reverse proxy, configured in `config/caddy/Caddyfile`.

## Setup

1. Install dependencies (see README-lightsail-setup.md)
2. Copy systemd services: `sudo cp config/systemd/*.service /etc/systemd/system/`
3. Copy Caddy config: `sudo cp config/caddy/Caddyfile /etc/caddy/`
4. Reload systemd: `sudo systemctl daemon-reload`
5. Start services: `sudo systemctl start jamulus-headless jackd`

## Configuration

- Environment variables are stored in `.env` (not tracked)
- AWS credentials in `.aws/` (not tracked)
- Service-specific config may be in individual files

## Documentation

- Fleet blueprint: `docs/FLEET-V1-BLUEPRINT.md`
- Ops dashboard: `docs/OPS_DASHBOARD.md`
- Support bot playbook: `docs/SUPPORT_PLAYBOOK.md`

## License

Private - PipeDreamers Project
