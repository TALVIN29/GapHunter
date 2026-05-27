"""
Engineered signal computation — PRD §7.2.
All computed at query time from raw Bright Data payload. No API calls.
"""

import math
import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%B %d, %Y",
    "%b %d, %Y",
    "%Y/%m/%d",
)


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except (ValueError, TypeError):
            continue
    # Fallback: ISO datetime prefix — strip to date portion
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def freshness_score(date_posted: str | None) -> float:
    """1 / (days_old + 1) — PRD §7.2. Unknown → 0.5 neutral."""
    d = _parse_date(date_posted)
    if d is None:
        return 0.5
    days_old = max(0, (date.today() - d).days)
    return round(1.0 / (days_old + 1), 4)


def competition_score(num_applicants: int | None) -> float:
    """1 / log10(applicants + 2) — PRD §7.2. Clamped [0, 1]. Unknown → 0.5."""
    if num_applicants is None:
        return 0.5
    raw = 1.0 / math.log10(max(0, num_applicants) + 2)
    return round(min(raw, 1.0), 4)


def compute_dual_signal(postings: list[dict]) -> None:
    """
    PRD §7.2: per-skill bool — skill confirmed in extracted_skills AND skills_listed.
    Mutates postings in-place. Call after extract_all() populates extracted_skills.
    """
    for p in postings:
        listed_lower = {s.strip().lower() for s in p.get("skills_listed", [])}
        p["dual_signal"] = {
            skill: skill.lower() in listed_lower
            for skill in p.get("extracted_skills", [])
        }


def compute_cross_source(postings: list[dict]) -> None:
    """
    PRD §7.2: skill confirmed in both LinkedIn AND Indeed sources (1.3x bonus).
    Mutates cross_source_confirmed in-place. Call after extract_all().
    """
    linkedin_skills: set[str] = set()
    indeed_skills: set[str] = set()

    for p in postings:
        skills = {s.lower() for s in p.get("extracted_skills", [])}
        if p.get("source") == "linkedin":
            linkedin_skills.update(skills)
        elif p.get("source") == "indeed":
            indeed_skills.update(skills)

    confirmed = linkedin_skills & indeed_skills

    for p in postings:
        p["cross_source_confirmed"] = {
            skill: skill.lower() in confirmed
            for skill in p.get("extracted_skills", [])
        }


def enrich_postings(postings: list[dict]) -> list[dict]:
    """
    Phase 1 enrichment — no skills required.
    Adds freshness_score, competition_score. Initialises signal dicts.
    Call before extract_all(). Returns same list (mutates in-place).
    """
    for p in postings:
        p["freshness_score"] = freshness_score(p.get("date_posted"))
        p["competition_score"] = competition_score(p.get("num_applicants"))
        p.setdefault("dual_signal", {})
        p.setdefault("cross_source_confirmed", {})
        p.setdefault("extracted_skills", [])
    return postings
