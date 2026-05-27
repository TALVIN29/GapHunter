"""
Async LLM extraction pipeline — PRD §5, Addendum A.
Claude Haiku 4.5 for cost efficiency (§10).
asyncio.Semaphore(10) caps concurrent calls (Addendum A §17).
"""

import asyncio
import json
import logging

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Addendum A: cap concurrent Claude Haiku calls — PRD §17
EXTRACTION_CONCURRENCY = 10
_semaphore = asyncio.Semaphore(EXTRACTION_CONCURRENCY)

_SYSTEM = "Extract technical skills from job descriptions. Return valid JSON only."

_USER_TEMPLATE = (
    "Extract all required technical skills from this job description.\n"
    "Return JSON only, no explanation: {{\"skills\": [\"skill1\", \"skill2\", ...]}}\n\n"
    "Job description:\n{job_description}"
)


def _parse_skills(text: str) -> list[str]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)["skills"]


async def extract_skills(
    client: anthropic.AsyncAnthropic,
    job_description: str,
) -> list[str]:
    """Single-pass extraction: job_description → list[str]. Pure fn, no side effects."""
    try:
        async with asyncio.timeout(15):
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": _USER_TEMPLATE.format(job_description=job_description),
                    }
                ],
            )
        return _parse_skills(response.content[0].text)
    except anthropic.RateLimitError:
        logger.warning("RateLimitError — sleeping 3s, single retry")
        await asyncio.sleep(3)
        try:
            async with asyncio.timeout(15):
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    system=_SYSTEM,
                    messages=[
                        {
                            "role": "user",
                            "content": _USER_TEMPLATE.format(
                                job_description=job_description
                            ),
                        }
                    ],
                )
            return _parse_skills(response.content[0].text)
        except Exception as exc:
            logger.warning("Retry failed: %s", exc)
            return []
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("Parse error: %s", exc)
        return []
    except asyncio.TimeoutError:
        logger.warning("TimeoutError after 15s")
        return []
    except Exception as exc:
        logger.warning("Unexpected error: %s", exc)
        return []


async def _extract_guarded(client: anthropic.AsyncAnthropic, jd: str) -> list[str]:
    """Semaphore-wrapped extraction — Addendum A."""
    async with _semaphore:
        return await extract_skills(client, jd)


async def extract_all(
    client: anthropic.AsyncAnthropic,
    job_descriptions: list[str],
) -> list[list[str]]:
    """Concurrent extraction, semaphore-capped at EXTRACTION_CONCURRENCY."""
    return list(
        await asyncio.gather(
            *[_extract_guarded(client, jd) for jd in job_descriptions]
        )
    )
