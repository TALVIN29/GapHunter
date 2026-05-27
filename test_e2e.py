"""
Day 27 E2E smoke test — real Bright Data + real Claude.
PRD §11: scrape -> isolate -> extract -> rank -> attach_evidence -> print gaps.
Manually verify top 3 gaps against source postings.
"""

import asyncio
import logging

import anthropic
from dotenv import load_dotenv

from extractor import extract_all
from pipeline import attach_evidence, format_output, isolate_job_descriptions, rank_gaps
from scraper import scrape_jobs

load_dotenv()
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

USER_SKILLS = "Python, SQL, pandas, scikit-learn"
JOB_ROLE    = "Data Analyst"
LOCATION    = "United States"


async def main() -> None:
    print(f"E2E smoke test — role: '{JOB_ROLE}' | skills: '{USER_SKILLS}'")
    print("Step 1: Scraping Bright Data...")

    postings = scrape_jobs(JOB_ROLE, LOCATION)
    print(f"  -> {len(postings)} postings returned")

    print("Step 2: Isolating job descriptions...")
    job_descriptions = isolate_job_descriptions(postings)
    print(f"  -> {len(job_descriptions)} valid descriptions")

    print("Step 3: Extracting skills (async concurrent Claude calls)...")
    client = anthropic.AsyncAnthropic()
    all_skills = await extract_all(client, job_descriptions)
    non_empty = sum(1 for s in all_skills if s)
    print(f"  -> {non_empty}/{len(job_descriptions)} descriptions yielded skills")

    print("Step 4: Ranking gaps...")
    gaps = rank_gaps(all_skills, USER_SKILLS)
    print(f"  -> {len(gaps)} gaps found")

    print("Step 5: Attaching evidence URLs...")
    evidence = attach_evidence(gaps, postings, all_skills)

    print("\n" + "=" * 50)
    print(format_output(evidence, total_postings=len(postings)))
    print("=" * 50)

    # Manual verification aid — print first 3 posting URLs + their extracted skills
    print("\n--- Verification: first 3 postings ---")
    for i, (posting, skills) in enumerate(zip(postings[:3], all_skills[:3]), 1):
        url = posting.get("url") or posting.get("job_url") or posting.get("link", "N/A")
        print(f"Posting {i}: {url}")
        print(f"  Skills: {skills}")


if __name__ == "__main__":
    asyncio.run(main())
