"""
Weighted scoring engine — PRD §7.3–7.4.
Replaces Counter() with multi-signal demand scoring.
"""

import logging
from statistics import mean

logger = logging.getLogger(__name__)


def skill_demand_score(skill: str, postings: list[dict]) -> float:
    """
    PRD §7.3: weighted demand score across all scraped postings.
    Signals: frequency(0.35) + freshness(0.25) + opportunity(0.20)
             + dual_source(0.10) + cross_source_bonus(0.10)
    """
    skill_lower = skill.lower()
    with_skill = [
        p for p in postings
        if skill_lower in {s.lower() for s in p.get("extracted_skills", [])}
    ]
    if not with_skill:
        return 0.0

    frequency = len(with_skill) / len(postings)
    freshness = mean(p["freshness_score"] for p in with_skill)
    opportunity = mean(p["competition_score"] for p in with_skill)
    dual_rate = mean(
        1.0 if p.get("dual_signal", {}).get(skill, False) else 0.0
        for p in with_skill
    )
    cross_bonus = (
        1.3
        if any(p.get("cross_source_confirmed", {}).get(skill) for p in with_skill)
        else 1.0
    )

    raw = (
        0.35 * frequency
        + 0.25 * freshness
        + 0.20 * opportunity
        + 0.10 * dual_rate
        + 0.10 * (cross_bonus - 1.0)
    )
    return round(raw * cross_bonus, 4)


def _skill_match_ratio(user_skills: set[str], posting_skills: list[str]) -> float:
    if not posting_skills:
        return 0.0
    posting_lower = {s.lower() for s in posting_skills}
    return len(user_skills & posting_lower) / len(posting_lower)


def _seniority_match(user_level: str, job_level: str) -> float:
    """Exact match → 1.0 | unknown → 0.7 | mismatch → 0.3. PRD §7.2."""
    if not job_level or job_level.lower() in ("unknown", ""):
        return 0.7
    return 1.0 if user_level.lower() == job_level.lower() else 0.3


def _remote_match(wants_remote: bool, is_remote: bool) -> float:
    """PRD §7.2. Not wanting remote → any mode fine (1.0)."""
    if not wants_remote:
        return 1.0
    return 1.0 if is_remote else 0.0


def _salary_in_range(salary_min: float, user_min: float) -> float:
    """PRD §7.2. 0.5 neutral when data missing."""
    if salary_min <= 0 or user_min <= 0:
        return 0.5
    return 1.0 if salary_min >= user_min else 0.0


def job_relevance_score(posting: dict, user_prefs: dict) -> float:
    """
    PRD §7.4: personalised job ranking.
    Weights: skill_match(0.40) + freshness(0.20) + competition(0.15)
             + seniority(0.10) + remote(0.10) + salary(0.05)
    """
    user_skills = {s.strip().lower() for s in user_prefs.get("skills", [])}
    skill_mr = _skill_match_ratio(user_skills, posting.get("extracted_skills", []))
    freshness = posting.get("freshness_score", 0.5)
    competition = posting.get("competition_score", 0.5)
    seniority = _seniority_match(
        user_prefs.get("seniority", "unknown"),
        posting.get("seniority_level", "unknown"),
    )
    remote = _remote_match(
        bool(user_prefs.get("remote", False)),
        bool(posting.get("is_remote", False)),
    )
    salary = _salary_in_range(
        float(posting.get("salary_min", 0)),
        float(user_prefs.get("salary_min", 0)),
    )
    return round(
        0.40 * skill_mr
        + 0.20 * freshness
        + 0.15 * competition
        + 0.10 * seniority
        + 0.10 * remote
        + 0.05 * salary,
        4,
    )


def rank_gaps(
    all_skills: list[list[str]],
    user_skills_raw: str,
    postings: list[dict],
    top_n: int = 5,
) -> list[dict]:
    """
    Weighted gap ranking — replaces Counter() in pipeline.py.
    Returns top_n gaps: [{"skill": str, "score": float}, ...]
    """
    user_set = {s.strip().lower() for s in user_skills_raw.split(",") if s.strip()}
    all_flat = {s.strip().lower() for sub in all_skills for s in sub}
    gaps = all_flat - user_set

    scored = [
        {"skill": skill, "score": skill_demand_score(skill, postings)}
        for skill in gaps
    ]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_n]


def rank_jobs(postings: list[dict], user_prefs: dict) -> list[dict]:
    """Attach relevance_score to each posting, sort descending."""
    for p in postings:
        p["relevance_score"] = job_relevance_score(p, user_prefs)
    postings.sort(key=lambda p: p["relevance_score"], reverse=True)
    return postings
