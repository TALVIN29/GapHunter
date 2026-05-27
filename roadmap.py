"""
Learning roadmap generation + pre-fetch cache — Addendum D, PRD §8.1 F4.
fire-and-forget via asyncio.create_task(). Polling endpoint always HTTP 200.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_TOKEN = os.environ.get("BRIGHTDATA_API_TOKEN", "")
_DATASET_SERP = os.environ.get("BRIGHTDATA_DATASET_ID", "")

# Addendum D: max 3 concurrent roadmap (Sonnet) calls
ROADMAP_SEMAPHORE = asyncio.Semaphore(3)

_SYSTEM = (
    "You are a senior learning coach. Generate concise, actionable learning roadmaps "
    "with real online resources. Return valid JSON only."
)

_USER_TEMPLATE = (
    "Create a learning roadmap for: {skill}\n\n"
    "Available learning resources found online:\n{resources}\n\n"
    "Return JSON only:\n"
    '{{"skill": "{skill}", '
    '"why_it_matters": "<1 sentence>", '
    '"steps": [{{"step": 1, "action": "<what to do>", "resource_url": "<url>", '
    '"duration": "<e.g. 2 weeks>"}}, ...], '
    '"estimated_total": "<e.g. 6 weeks>"}}'
)


class RoadmapStatus(str, Enum):
    PENDING = "pending"
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"


@dataclass
class RoadmapEntry:
    status: RoadmapStatus = RoadmapStatus.PENDING
    roadmap: dict | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.monotonic)


# session_id → {skill → RoadmapEntry}
ROADMAP_CACHE: dict[str, dict[str, RoadmapEntry]] = {}


def init_roadmap_cache(session_id: str, skills: list[str]) -> None:
    """Call immediately after F2 search completes. Creates PENDING entries."""
    ROADMAP_CACHE[session_id] = {skill: RoadmapEntry() for skill in skills}


def get_roadmap_status(session_id: str, skill: str) -> dict:
    """Polling endpoint payload. Always returns HTTP 200 — PRD Addendum D."""
    session = ROADMAP_CACHE.get(session_id, {})
    entry = session.get(skill)
    if not entry:
        return {"status": "not_found"}
    if entry.status == RoadmapStatus.READY:
        return {"status": "ready", "roadmap": entry.roadmap}
    if entry.status == RoadmapStatus.FAILED:
        return {"status": "failed", "error": "generation failed"}
    return {"status": entry.status.value}


def _fetch_learning_resources(skill: str) -> list[str]:
    """SERP call — find top learning URLs for this skill."""
    if not _TOKEN or not _DATASET_SERP:
        return []
    keyword = f"{skill} course tutorial site:coursera.org OR site:udemy.com OR site:youtube.com"
    payload = {
        "input": [
            {
                "url": "https://www.google.com/",
                "keyword": keyword,
                "language": "en",
                "tbs": "",
                "tbm": "",
                "uule": "",
                "nfpr": "",
                "index": "",
            }
        ]
    }
    try:
        resp = requests.post(
            f"https://api.brightdata.com/datasets/v3/scrape"
            f"?dataset_id={_DATASET_SERP}&notify=false&include_errors=true",
            headers={"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        organic = data.get("organic", []) if isinstance(data, dict) else []
        return [r["link"] for r in organic[:5] if r.get("link")]
    except Exception as exc:
        logger.warning("SERP resource fetch failed for %s: %s", skill, exc)
        return []


async def _generate_roadmap(
    client: anthropic.AsyncAnthropic,
    skill: str,
    job_descriptions: list[str],
) -> dict | None:
    """Single roadmap generation: SERP resources → Claude Sonnet synthesis."""
    resources = await asyncio.to_thread(_fetch_learning_resources, skill)
    resources_text = "\n".join(f"- {u}" for u in resources) or "No specific resources found"

    prompt = _USER_TEMPLATE.format(skill=skill, resources=resources_text)
    try:
        async with asyncio.timeout(30):
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except asyncio.TimeoutError:
        logger.warning("Roadmap generation timed out for %s", skill)
        return None
    except Exception as exc:
        logger.warning("Roadmap generation failed for %s: %s", skill, exc)
        return None


async def _prefetch_one(
    client: anthropic.AsyncAnthropic,
    session_id: str,
    skill: str,
    job_descriptions: list[str],
) -> None:
    """Generate roadmap for one skill, write result to cache."""
    entry = ROADMAP_CACHE.get(session_id, {}).get(skill)
    if not entry:
        return
    entry.status = RoadmapStatus.GENERATING
    async with ROADMAP_SEMAPHORE:
        result = await _generate_roadmap(client, skill, job_descriptions)
    if result:
        entry.status = RoadmapStatus.READY
        entry.roadmap = result
    else:
        entry.status = RoadmapStatus.FAILED


async def prefetch_roadmaps(
    client: anthropic.AsyncAnthropic,
    session_id: str,
    skills: list[str],
    job_descriptions: list[str],
) -> None:
    """
    Addendum D: fire-and-forget task launched after F2 returns.
    All skills generated concurrently (bounded by ROADMAP_SEMAPHORE=3).
    """
    try:
        await asyncio.gather(
            *[_prefetch_one(client, session_id, skill, job_descriptions) for skill in skills]
        )
        logger.info("Roadmap prefetch complete for session %s", session_id)
    except Exception as exc:
        logger.warning("Roadmap prefetch error session %s: %s", session_id, exc)
