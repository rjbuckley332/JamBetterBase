#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone

OUT_JSON = "/tmp/jambetter_health.json"
ALERT_LOG = "/tmp/jambetter_alerts.log"

SERVICES = [
    "jamulus-headless.service",
    "trackbot-web.service",
    "jamulus-toggle-webapp.service",
    "jamulus-uploader.service",
]


def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, (p.stdout or "").strip()


def service_state(name: str):
    rc, out = run(["systemctl", "is-active", name])
    active = (rc == 0 and out == "active")
    return {"name": name, "active": active, "state": out}


def load_stats():
    try:
        l1, l5, l15 = os.getloadavg()
    except Exception:
        l1, l5, l15 = (0.0, 0.0, 0.0)
    cores = os.cpu_count() or 1
    ratio = l1 / max(1, cores)
    return {
        "load1": round(l1, 2),
        "load5": round(l5, 2),
        "load15": round(l15, 2),
        "cores": int(cores),
        "load_ratio": round(ratio, 2),
    }


def disk_stats(path="/"):
    du = shutil.disk_usage(path)
    used_pct = (du.used / du.total) * 100.0 if du.total else 0.0
    return {
        "path": path,
        "total_gb": round(du.total / (1024**3), 1),
        "free_gb": round(du.free / (1024**3), 1),
        "used_pct": round(used_pct, 1),
    }


def quality_grade(services, load_ratio, disk_used_pct):
    if any(not s["active"] for s in services):
        return "red", "Service issue"
    if disk_used_pct >= 90:
        return "red", "Disk critical"
    if load_ratio >= 1.10:
        return "yellow", "Busy"
    return "green", "Good"


def main():
    now = datetime.now(timezone.utc).isoformat()
    services = [service_state(s) for s in SERVICES]
    load = load_stats()
    disk = disk_stats("/")
    grade, label = quality_grade(services, load["load_ratio"], disk["used_pct"])

    data = {
        "ok": True,
        "ts": now,
        "quality": {"grade": grade, "label": label},
        "services": services,
        "load": load,
        "disk": disk,
    }

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    if grade in ("red", "yellow"):
        with open(ALERT_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{now}] {grade.upper()} {label} load={load[load_ratio]} disk={disk[used_pct]}\\n")


if __name__ == "__main__":
    main()
