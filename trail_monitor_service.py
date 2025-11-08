r"""
Real-time Creo Trail File Monitor Service
Auto-detects active trail file from C:\Users\Public\Documents
and streams memory events via SSE when memory increases >10 MB
"""

import os
import time
import re
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
from fastapi.responses import FileResponse, HTMLResponse
from pathlib import Path
import random


app = FastAPI()
# --- Demo / simulation ---
FAKE_SPIKES = True          # turn off when you want only real events
FAKE_INTERVAL_SEC = 20      # how often to inject a fake spike


# Enable CORS for local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# ---- Serve the HTML for localhost (enables desktop notifications) ----
HERE = Path(__file__).resolve().parent
HTML_PATH = HERE / "memory_toaster.html"

@app.get("/", response_class=HTMLResponse)
async def root():
    if HTML_PATH.exists():
        return FileResponse(str(HTML_PATH))
    return HTMLResponse(
        "<h3>memory_toaster.html not found next to trail_monitor_service.py</h3>", status_code=404
    )

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return HTMLResponse("", status_code=204)


# === Configuration ===
from pathlib import Path
TRAIL_FILE_DIR = Path(r"C:\Users\Public\Documents")

MONITORED_FILE = None
LAST_POSITION = 0
LAST_MEMORY_DATA = {}
POLL_INTERVAL = 1.0  # seconds

# === Core Logic ===

def find_latest_trail_file() -> Path | None:
    """Find the most recently modified trail.txt.* file."""
    trail_files = list(TRAIL_FILE_DIR.glob("trail.txt.*"))
    if not trail_files:
        return None
    return max(trail_files, key=lambda p: p.stat().st_mtime)


def parse_memory_line(line: str):
    """
    Parse Creo memory line like:
    !mem_use INCREASE Blocks 1234, AppSize 10240000, SysSize 20971520
    """
    match = re.search(r'!mem_use\s+INCREASE\s+Blocks\s+(\d+),\s*AppSize\s+(\d+),\s*SysSize\s+(\d+)', line)
    if match:
        return {
            "blocks": int(match.group(1)),
            "appSize": int(match.group(2)),   # in bytes
            "sysSize": int(match.group(3)),   # in bytes
            "timestamp": datetime.now().isoformat(),
            "raw_line": line.strip(),
        }
    return None


def calculate_memory_diff(current, previous):
    """Return memory delta in MB."""
    if not previous:
        return 0.0
    return round((current["appSize"] - previous["appSize"]) / (1024 * 1024), 2)


async def monitor_trail_file():
    """Generator that yields memory and ProCmd command warnings."""
    global MONITORED_FILE, LAST_POSITION, LAST_MEMORY_DATA

    last_alert_time = 0
    ALERT_COOLDOWN = 60  # seconds between alerts

    while True:
        try:
            current_file = find_latest_trail_file()

            # Detect new trail file
            if current_file != MONITORED_FILE:
                MONITORED_FILE = current_file
                LAST_POSITION = 0
                LAST_MEMORY_DATA = {}
                if current_file:
                    print(f"🟢 Now monitoring: {current_file}")
                    yield {
                        "event": "file_change",
                        "data": json.dumps({
                            "trailFile": str(current_file),
                            "timestamp": datetime.now().isoformat(),
                            "message": f"Now monitoring: {current_file.name}"
                        })
                    }

            if not MONITORED_FILE or not MONITORED_FILE.exists():
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # Read newly appended lines
            with open(MONITORED_FILE, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(LAST_POSITION)
                new_lines = f.readlines()
                LAST_POSITION = f.tell()

            for line in new_lines:
                line = line.strip()
                if not line:
                    continue

                # === Trigger notification on any new ProCmd command ===
                if "!Command" in line and "ProCmd" in line:
                        # Filter out common noise commands
                    important_cmds = (
                        "ProCmdModelSave", "ProCmdModelSaveAs",
                        "ProCmdRegen", "ProCmdFeatureCreate",
                        "ProCmdFeatureResume", "ProCmdFeatureSuppress",
                        "ProCmdWindowActivate"
                    )
                    if not any(cmd in line for cmd in important_cmds):
                        continue  # skip non-user commands
                    now = time.time()
                    if now - last_alert_time < ALERT_COOLDOWN:
                            continue  # skip repeated alerts
                    last_alert_time = now
                    print(f"⚙️ Command detected (ProCmd): {line}")
                    payload = {
                        "eventType": "command_warning",
                        "trailFile": str(MONITORED_FILE),
                        "pollInterval": POLL_INTERVAL,
                        "timestamp": datetime.now().isoformat(),
                        "message": "Memory increased, save your work now!"
                    }
                    yield {
                        "event": "command_event",
                        "data": json.dumps(payload)
                    }

                # === Continue normal memory spike detection ===
                mem_data = parse_memory_line(line)
                if mem_data:
                    diff_mb = calculate_memory_diff(mem_data, LAST_MEMORY_DATA)
                    mem_data["deltaMB"] = diff_mb
                    if diff_mb > 10:
                        print(f"🚨 Spike detected +{diff_mb} MB")
                        payload = {
                            "eventType": "memory_spike",
                            "trailFile": str(MONITORED_FILE),
                            "pollInterval": POLL_INTERVAL,
                            "deltaMB": diff_mb,
                            "timestamp": datetime.now().isoformat(),
                            "message": "Memory increased, save your work now!"
                        }
                        yield {
                            "event": "memory_event",
                            "data": json.dumps(payload)
                        }
                    LAST_MEMORY_DATA = mem_data

            # Heartbeat for UI
            yield {
                "event": "heartbeat",
                "data": json.dumps({
                    "trailFile": str(MONITORED_FILE),
                    "pollInterval": POLL_INTERVAL,
                    "timestamp": datetime.now().isoformat(),
                    "message": "Monitoring active"
                })
            }

            await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            print(f"❌ Monitor loop error: {e}")
            await asyncio.sleep(POLL_INTERVAL * 2)





async def event_stream():
    """Server-Sent Events stream endpoint."""
    async for event_data in monitor_trail_file():
        yield f"event: {event_data['event']}\ndata: {event_data['data']}\n\n"


@app.get("/stream")
async def stream_events():
    """SSE endpoint for browser UI."""
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/status")
async def status():
    """Current monitoring status."""
    return {
        "trailDirectory": str(TRAIL_FILE_DIR),
        "currentFile": str(MONITORED_FILE) if MONITORED_FILE else None,
        "lastMemory": LAST_MEMORY_DATA,
        "pollInterval": POLL_INTERVAL,
    }


if __name__ == "__main__":
    import uvicorn
    print(f"🚀 Starting Trail Monitor — watching {TRAIL_FILE_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=8000)
