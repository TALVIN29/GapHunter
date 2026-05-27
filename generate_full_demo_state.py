"""
PRD §30.4 — Addendum O: Golden Path Lock.
Captures a live /api/search response + pre-generated roadmaps as static demo state.
Calls live Render endpoint directly — avoids ASGI startup event issues.

Usage:
    python generate_full_demo_state.py
Requires: DEMO_SECRET in .env, Render deployed and warm, circuit_open=False.

Output: fallback/demo_state_data_analyst.json with keys:
  jobs, gaps, session_id, roadmaps (dict skill->roadmap for startup pre-seeding)
"""

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

RENDER_URL   = "https://gaphunter-api.onrender.com"
TARGET_ROLE  = "Data Analyst"
OUTPUT_PATH  = Path(__file__).parent / "fallback" / "demo_state_data_analyst.json"
DEMO_SECRET  = os.environ.get("DEMO_SECRET", "")
ROADMAP_POLL_TIMEOUT = 180   # seconds to wait for all roadmaps to generate
ROADMAP_POLL_INTERVAL = 5


def _headers() -> dict:
    return {"X-Demo-Secret": DEMO_SECRET, "Content-Type": "application/json"}


def _poll_roadmaps(real_session_id: str, gap_skills: list[str]) -> dict:
    """Poll /api/roadmap/{skill}?session_id={real_session_id} until all READY or timeout."""
    roadmaps: dict = {}
    remaining = list(gap_skills)
    deadline = time.time() + ROADMAP_POLL_TIMEOUT

    print(f"  Polling roadmaps for {len(gap_skills)} skills (timeout {ROADMAP_POLL_TIMEOUT}s)...")
    while remaining and time.time() < deadline:
        still_pending = []
        for skill in remaining:
            try:
                resp = requests.get(
                    f"{RENDER_URL}/api/roadmap/{quote(skill)}",
                    params={"session_id": real_session_id},
                    headers={"X-Demo-Secret": DEMO_SECRET},
                    timeout=20,
                )
                result = resp.json()
                if result.get("status") == "ready":
                    roadmaps[skill] = result.get("roadmap", {})
                    print(f"    {skill}: READY")
                else:
                    still_pending.append(skill)
            except Exception as exc:
                print(f"    {skill}: poll error ({exc}) — retrying", file=sys.stderr)
                still_pending.append(skill)

        remaining = still_pending
        if remaining:
            time.sleep(ROADMAP_POLL_INTERVAL)

    if remaining:
        print(
            f"  WARNING: roadmaps not ready after {ROADMAP_POLL_TIMEOUT}s: {remaining}",
            file=sys.stderr,
        )
    return roadmaps


def capture() -> None:
    if not DEMO_SECRET:
        print("ERROR: DEMO_SECRET not set in .env", file=sys.stderr)
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Step 1: check circuit is open=False (live path required for real data)
    health_resp = requests.get(f"{RENDER_URL}/health", timeout=20)
    if health_resp.status_code != 200:
        print(f"FAIL: /health returned {health_resp.status_code}", file=sys.stderr)
        sys.exit(1)
    health = health_resp.json()
    if health.get("circuit_open"):
        print(
            "FAIL: circuit_open=true on Render — live pipeline unavailable.\n"
            "Reset CIRCUIT_BREAKER_LIMIT to 100 and redeploy before running this script.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"Health check OK — circuit_open=false, uptime={health.get('uptime_human')}")

    # Step 2: live search
    print(f"Capturing live search for '{TARGET_ROLE}' ...")
    resp = requests.post(
        f"{RENDER_URL}/api/search",
        json={"role": TARGET_ROLE, "location": "United States"},
        headers=_headers(),
        timeout=120,
    )
    if resp.status_code != 200:
        print(f"FAIL: /api/search HTTP {resp.status_code}", file=sys.stderr)
        print(resp.text[:500], file=sys.stderr)
        sys.exit(1)

    data = resp.json()
    if data.get("status") != "ok":
        print(f"FAIL: status={data.get('status')} — {data.get('message', '')}", file=sys.stderr)
        sys.exit(1)

    assert "jobs" in data and len(data["jobs"]) >= 5, f"Need >=5 jobs, got {len(data.get('jobs', []))}"
    assert "gaps" in data and len(data["gaps"]) >= 3, f"Need >=3 gaps, got {len(data.get('gaps', []))}"

    real_session_id = data["session_id"]   # UUID from live pipeline
    gap_skills = [g["skill"] for g in data["gaps"]]
    print(f"  session_id (live): {real_session_id}")
    print(f"  jobs: {len(data['jobs'])}")
    print(f"  gaps: {gap_skills}")
    print(f"  TOP GAP: {gap_skills[0]}")

    # Step 3: poll roadmaps (pre-fetch fired by search endpoint)
    roadmaps = _poll_roadmaps(real_session_id, gap_skills)

    # Step 4: override session_id + attach roadmaps
    data["session_id"] = "demo-static"
    data["roadmaps"] = roadmaps

    OUTPUT_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"\nSaved -> {OUTPUT_PATH}")
    print(f"  jobs: {len(data['jobs'])}")
    print(f"  gaps: {gap_skills}")
    print(f"  roadmaps captured: {list(roadmaps.keys())}")
    print(f"  TOP GAP: {gap_skills[0]}")
    print(f"  size: {size_kb:.1f} KB")

    if not roadmaps:
        print(
            "\nWARNING: no roadmaps captured — demo-static roadmap pre-seeding will be skipped.\n"
            "Re-run after waiting longer for roadmap generation, or check Render logs.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    capture()
