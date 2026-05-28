"""
Two-step multi-source scraper — PRD §7.1, §7.6.
Step 1: Google SERP → LinkedIn job URLs.
Step 2: LinkedIn Collect + Indeed (parallel) → full payload with all §7.1 fields.
Shadow Mode wrapper (Addendum C) lives in api.py.
"""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

POSTINGS_CAP = 10
SERP_TIMEOUT = 55
LINKEDIN_TIMEOUT = 45
INDEED_TIMEOUT = 45

_TOKEN = os.environ.get("BRIGHTDATA_API_TOKEN", "")
_DATASET_SERP = os.environ.get("BRIGHTDATA_DATASET_ID", "")
_DATASET_LINKEDIN = os.environ.get("BRIGHTDATA_DATASET_ID_LINKEDIN", "")
_DATASET_INDEED = os.environ.get("BRIGHTDATA_DATASET_ID_INDEED", "")

_API_BASE = "https://api.brightdata.com/datasets/v3/scrape"


def _scrape_url(dataset_id: str) -> str:
    return f"{_API_BASE}?dataset_id={dataset_id}&notify=false&include_errors=true"


def scrape_jobs(job_role: str, location: str) -> list[dict]:
    """
    Full pipeline: SERP → LinkedIn + Indeed (parallel).
    Returns up to POSTINGS_CAP quality-filtered, normalised postings.
    PRD §7.1, §7.6.
    """
    _check_env()

    t0 = time.time()
    job_urls = _step1_serp(job_role, location)
    logger.info("Step 1: %d URLs in %.1fs", len(job_urls), time.time() - t0)

    t1 = time.time()
    postings = _step2_multi_source(job_urls[:POSTINGS_CAP], job_role, location)
    logger.info("Step 2: %d postings in %.1fs", len(postings), time.time() - t1)

    return _quality_filter(postings)


def _step1_serp(job_role: str, location: str) -> list[str]:
    """SERP query → individual LinkedIn job view URLs."""
    keyword = f"{job_role} jobs {location} site:linkedin.com/jobs/view"
    payload = {
        "input": [
            {
                "url": "https://www.google.com/",
                "keyword": keyword,
                "language": "en",
                "tbs": "qdr:m",
                "tbm": "",
                "uule": "",
                "nfpr": "",
                "index": "",
            }
        ]
    }
    resp = requests.post(
        _scrape_url(_DATASET_SERP),
        headers=_headers(),
        json=payload,
        timeout=SERP_TIMEOUT,
    )
    resp.raise_for_status()

    data = resp.json()
    organic = data.get("organic", []) if isinstance(data, dict) else []
    urls = [
        r["link"]
        for r in organic
        if "linkedin.com/jobs/view" in r.get("link", "")
    ]
    logger.info("SERP returned %d LinkedIn job URLs", len(urls))
    return urls


def _step2_multi_source(
    job_urls: list[str], job_role: str, location: str
) -> list[dict]:
    """Run LinkedIn Collect + Indeed in parallel via ThreadPoolExecutor."""
    results: list[dict] = []

    futures_map = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        if job_urls and _DATASET_LINKEDIN:
            futures_map["linkedin"] = executor.submit(_fetch_linkedin, job_urls)
        if _DATASET_INDEED:
            futures_map["indeed"] = executor.submit(
                _fetch_indeed, job_role, location
            )

    for source, future in futures_map.items():
        try:
            results.extend(future.result())
        except Exception as exc:
            logger.warning("Source %s failed: %s", source, exc)

    return results


def _fetch_linkedin(job_urls: list[str]) -> list[dict]:
    payload = {"input": [{"url": u} for u in job_urls]}
    resp = requests.post(
        _scrape_url(_DATASET_LINKEDIN),
        headers=_headers(),
        json=payload,
        timeout=LINKEDIN_TIMEOUT,
    )
    resp.raise_for_status()

    records = _parse_ndjson(resp.text)
    return [_normalise_linkedin(r) for r in records]


def _fetch_indeed(job_role: str, location: str) -> list[dict]:
    payload = {
        "input": [
            {
                "keyword": job_role,
                "location": location or "",
                "num_of_results": POSTINGS_CAP,
            }
        ]
    }
    resp = requests.post(
        _scrape_url(_DATASET_INDEED),
        headers=_headers(),
        json=payload,
        timeout=INDEED_TIMEOUT,
    )
    resp.raise_for_status()

    records = _parse_ndjson(resp.text)
    return [_normalise_indeed(r) for r in records]


def _parse_ndjson(text: str) -> list[dict]:
    records = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed NDJSON line")
    return records


def _normalise_linkedin(r: dict) -> dict:
    """Map LinkedIn Collect field names → standard posting dict (PRD §7.1 full field set)."""
    salary_min = _safe_float(r.get("salary_base_min"))
    salary_max = _safe_float(r.get("salary_base_max"))
    num_applicants = _safe_int(r.get("number_of_applicants"))

    remote_type = (r.get("remote_type") or "").lower()
    is_remote = remote_type in ("remote", "fully remote")

    seniority = (r.get("job_seniority_level") or "unknown").lower()

    return {
        # Core text
        "job_description": r.get("job_summary") or "",
        "title": r.get("job_title") or "",
        "company_name": r.get("company_name") or "",
        "url": r.get("url") or r.get("apply_link") or "",
        "apply_link": r.get("apply_link") or r.get("url") or "",
        "location": r.get("job_location") or "",
        # LinkedIn taxonomy (for dual_signal)
        "skills_listed": r.get("skills_listed") or [],
        # Signals
        "date_posted": r.get("job_posted_date") or "",
        "num_applicants": num_applicants,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": r.get("salary_currency") or "USD",
        "is_remote": is_remote,
        "remote_type": remote_type,
        "seniority_level": seniority,
        "employment_type": (r.get("job_employment_type") or "").lower(),
        # Company metadata (HR tab)
        "company_industry": r.get("company_industry") or "",
        "company_size": r.get("company_size") or "",
        "company_description": r.get("company_description") or "",
        # Source tag for cross-source validation
        "source": "linkedin",
        # Populated by extractor + signals
        "extracted_skills": [],
        "freshness_score": 0.5,
        "competition_score": 0.5,
        "dual_signal": {},
        "cross_source_confirmed": {},
    }


def _normalise_indeed(r: dict) -> dict:
    """Map Indeed field names → standard posting dict. Flexible fallbacks for schema variation."""
    salary_min = _safe_float(
        r.get("salary_base_min") or r.get("salary_min") or r.get("min_salary")
    )
    salary_max = _safe_float(
        r.get("salary_base_max") or r.get("salary_max") or r.get("max_salary")
    )
    num_applicants = _safe_int(r.get("number_of_applicants") or r.get("num_applicants"))

    remote_raw = (
        r.get("remote_type") or r.get("work_type") or r.get("is_remote") or ""
    )
    is_remote = str(remote_raw).lower() in ("remote", "fully remote", "true", "1")

    seniority = (
        r.get("job_seniority_level") or r.get("seniority_level") or "unknown"
    ).lower()

    description = (
        r.get("job_description")
        or r.get("description")
        or r.get("job_summary")
        or ""
    )
    title = r.get("job_title") or r.get("title") or ""
    url = r.get("url") or r.get("job_url") or r.get("apply_link") or ""
    date_posted = r.get("job_posted_date") or r.get("date_posted") or r.get("posted_at") or ""

    return {
        "job_description": description,
        "title": title,
        "company_name": r.get("company_name") or r.get("company") or "",
        "url": url,
        "apply_link": url,
        "location": r.get("job_location") or r.get("location") or "",
        "skills_listed": r.get("skills_listed") or [],
        "date_posted": date_posted,
        "num_applicants": num_applicants,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": r.get("salary_currency") or "USD",
        "is_remote": is_remote,
        "remote_type": str(remote_raw).lower(),
        "seniority_level": seniority,
        "employment_type": (r.get("job_employment_type") or r.get("employment_type") or "").lower(),
        "company_industry": r.get("company_industry") or "",
        "company_size": r.get("company_size") or "",
        "company_description": r.get("company_description") or "",
        "source": "indeed",
        "extracted_skills": [],
        "freshness_score": 0.5,
        "competition_score": 0.5,
        "dual_signal": {},
        "cross_source_confirmed": {},
    }


def _quality_filter(records: list[dict]) -> list[dict]:
    """Keep postings with job_description >= 200 chars + title + company."""
    qualified = [
        r for r in records
        if len(r.get("job_description", "").strip()) >= 200
        and r.get("title", "").strip()
        and r.get("company_name", "").strip()
    ]
    qualified.sort(key=lambda r: len(r.get("job_description", "")), reverse=True)
    kept = qualified[:POSTINGS_CAP]
    logger.info("Quality filter: %d/%d kept", len(kept), len(records))
    return kept


def _safe_float(val) -> float:
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _safe_int(val) -> int | None:
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "Content-Type": "application/json",
    }


def _check_env() -> None:
    if not _TOKEN:
        raise EnvironmentError("BRIGHTDATA_API_TOKEN not set in .env")
    if not _DATASET_SERP:
        raise EnvironmentError("BRIGHTDATA_DATASET_ID not set in .env")
    if not _DATASET_LINKEDIN and not _DATASET_INDEED:
        raise EnvironmentError(
            "At least one of BRIGHTDATA_DATASET_ID_LINKEDIN or BRIGHTDATA_DATASET_ID_INDEED must be set"
        )
