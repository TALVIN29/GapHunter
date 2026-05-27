"""
PRD §30.4 — Addendum O: Golden Path Lock.
Captures a live /api/search response as static demo state.
Run once before demo recording. Requires DEMO_SECRET env var.

Usage:
    DEMO_SECRET=<secret> python generate_full_demo_state.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from api import app
from httpx import AsyncClient

TARGET_ROLE = "Data Analyst"
OUTPUT_PATH = Path(__file__).parent / "fallback" / "demo_state_data_analyst.json"
DEMO_SECRET = os.environ.get("DEMO_SECRET", "")


async def capture() -> None:
    if not DEMO_SECRET:
        print("ERROR: DEMO_SECRET env var not set", file=sys.stderr)
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with AsyncClient(app=app, base_url="http://test") as client:
        print(f"Capturing live search for '{TARGET_ROLE}' …")
        resp = await client.post(
            "/api/search",
            json={"role": TARGET_ROLE, "location": "United States"},
            headers={"X-Demo-Secret": DEMO_SECRET},
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()

    assert data.get("status") == "ok", f"Search failed: {data}"
    assert "jobs" in data and len(data["jobs"]) >= 5, "Need ≥5 jobs"
    assert "gaps" in data and len(data["gaps"]) >= 3, "Need ≥3 gaps"

    data["session_id"] = "demo-static"
    OUTPUT_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved → {OUTPUT_PATH}")
    print(f"  jobs: {len(data['jobs'])}, gaps: {len(data['gaps'])}")


if __name__ == "__main__":
    asyncio.run(capture())
