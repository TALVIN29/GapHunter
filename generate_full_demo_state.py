"""
PRD §30.4 — Addendum O: Golden Path Lock.
Captures a live /api/search response as static demo state.
Calls live Render endpoint directly — avoids ASGI startup event issues.

Usage:
    python generate_full_demo_state.py
Requires: DEMO_SECRET in .env, Render deployed and warm.
"""

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

RENDER_URL   = "https://gaphunter-api.onrender.com"
TARGET_ROLE  = "Data Analyst"
OUTPUT_PATH  = Path(__file__).parent / "fallback" / "demo_state_data_analyst.json"
DEMO_SECRET  = os.environ.get("DEMO_SECRET", "")


def capture() -> None:
    if not DEMO_SECRET:
        print("ERROR: DEMO_SECRET not set in .env", file=sys.stderr)
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    print(f"Capturing live search for '{TARGET_ROLE}' via {RENDER_URL} …")
    resp = requests.post(
        f"{RENDER_URL}/api/search",
        json={"role": TARGET_ROLE, "location": "United States"},
        headers={
            "X-Demo-Secret": DEMO_SECRET,
            "Content-Type": "application/json",
        },
        timeout=120,
    )

    if resp.status_code != 200:
        print(f"FAIL: HTTP {resp.status_code}", file=sys.stderr)
        print(resp.text[:500], file=sys.stderr)
        sys.exit(1)

    data = resp.json()

    if data.get("status") != "ok":
        print(f"FAIL: status={data.get('status')} — {data.get('message', '')}", file=sys.stderr)
        sys.exit(1)

    assert "jobs" in data and len(data["jobs"]) >= 5, f"Need ≥5 jobs, got {len(data.get('jobs', []))}"
    assert "gaps" in data and len(data["gaps"]) >= 3, f"Need ≥3 gaps, got {len(data.get('gaps', []))}"

    data["session_id"] = "demo-static"
    OUTPUT_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Saved -> {OUTPUT_PATH}")
    print(f"  jobs: {len(data['jobs'])}")
    print(f"  gaps: {[g['skill'] for g in data['gaps']]}")
    print(f"  TOP GAP: {data['gaps'][0]['skill']}")
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"  size: {size_kb:.1f} KB")


if __name__ == "__main__":
    capture()
