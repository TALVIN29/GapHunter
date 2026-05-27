"""
Integration pipeline — Day 27
PRD §7: isolate -> extract -> rank -> attach_evidence
Flat Harness: no frameworks, explicit steps only.
"""

import logging
from collections import Counter

logger = logging.getLogger(__name__)


def isolate_job_descriptions(payload: list[dict]) -> list[str]:
    """
    Extract valid job_description strings from Bright Data payload.
    PRD §7 step 4: drop missing/None/short records, guard < 3 valid.
    """
    valid = []
    dropped = 0

    for record in payload:
        jd = record.get("job_description")
        if not jd or not isinstance(jd, str) or len(jd.strip()) < 200:
            dropped += 1
            continue
        valid.append(jd.strip())

    if dropped:
        logger.warning("Dropped %d records (missing/short job_description)", dropped)

    if len(valid) < 3:
        raise ValueError(
            f"Scrape returned {len(valid)} valid records — minimum 3 required"
        )

    return valid


def rank_gaps(
    all_skills: list[list[str]],
    user_skills_raw: str,
) -> list[tuple[str, int]]:
    """
    PRD §7 step 6: deterministic Counter + set subtraction. No embeddings, no fuzzy.
    Returns top 5 gaps sorted by frequency descending.
    """
    all_flat = [s.strip().lower() for sub in all_skills for s in sub]
    counts = Counter(all_flat)
    user_set = {s.strip().lower() for s in user_skills_raw.split(",") if s.strip()}
    gaps = {skill: n for skill, n in counts.items() if skill not in user_set}
    return sorted(gaps.items(), key=lambda x: x[1], reverse=True)[:5]


def attach_evidence(
    gaps: list[tuple[str, int]],
    postings: list[dict],
    extracted_skills: list[list[str]],
) -> list[dict]:
    """
    PRD §7 step 8: per gap skill, find up to 3 posting URLs where that skill appeared.
    Returns: [{"skill": "dbt", "count": 14, "urls": [...]}]
    """
    result = []
    for skill, count in gaps:
        urls = []
        for posting, skills in zip(postings, extracted_skills):
            if skill in [s.strip().lower() for s in skills]:
                url = posting.get("url") or posting.get("job_url") or posting.get("link", "")
                if url and url not in urls:
                    urls.append(url)
            if len(urls) == 3:
                break
        result.append({"skill": skill, "count": count, "urls": urls})
    return result


def format_output(evidence: list[dict], total_postings: int) -> str:
    """
    PRD §8 output format: GAP #N: skill (mentioned in X/Y postings) + URLs.
    """
    if not evidence:
        return "No skill gaps found — your skills match all scraped postings."

    lines = []
    for i, item in enumerate(evidence, 1):
        display_skill = item["skill"].title()
        lines.append(
            f"GAP #{i}: {display_skill} (mentioned in {item['count']}/{total_postings} postings)"
        )
        for url in item["urls"]:
            lines.append(f"  -> {url}")
        if not item["urls"]:
            lines.append("  -> (no URLs available for this gap)")
        lines.append("")
    return "\n".join(lines).strip()
