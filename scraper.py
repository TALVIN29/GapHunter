"""
Multi-source verified job aggregator — PRD §7.1, §7.6, Addendum T.

Pipeline:
  Step 1: Bright Data SERP — location-aware Google queries → LinkedIn job view URLs
  Step 2: LinkedIn Collect + Indeed direct API (parallel) → full posting payload
  Step 3: Scam detection filter — removes fake/suspicious postings
  Step 4: Deduplication — removes cross-source duplicates by (title, company)
  Step 5: Quality filter — description length + required fields

Value over plain LinkedIn search:
  - Multi-source aggregation (LinkedIn + Indeed simultaneously)
  - AI-driven scam detection (keyword patterns + salary anomalies)
  - Cross-platform deduplication
  - Structured output for AI skill extraction + demand scoring
"""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

POSTINGS_CAP = 10
SERP_TIMEOUT = 75       # Bright Data SERP takes 30-60s — wait it out
SERP_POLL_MAX_S = 70    # poll budget if async snapshot returned
LINKEDIN_TIMEOUT = 55
INDEED_TIMEOUT = 45

_TOKEN = os.environ.get("BRIGHTDATA_API_TOKEN", "")
_DATASET_SERP = os.environ.get("BRIGHTDATA_DATASET_ID", "")
_DATASET_LINKEDIN = os.environ.get("BRIGHTDATA_DATASET_ID_LINKEDIN", "")
_DATASET_INDEED = os.environ.get("BRIGHTDATA_DATASET_ID_INDEED", "")

_API_BASE = "https://api.brightdata.com/datasets/v3/scrape"

# ---------------------------------------------------------------------------
# Scam detection — Addendum T
# ---------------------------------------------------------------------------

_SCAM_DESC_KEYWORDS: frozenset[str] = frozenset({
    "no experience needed",
    "work from home easy",
    "earn from home",
    "unlimited earning",
    "be your own boss",
    "passive income",
    "network marketing",
    "make money fast",
    "guaranteed income",
    "per hour from home",
    "whatsapp me",
    "send cv to whatsapp",
    "send resume to telegram",
    "no investment required",
    "direct income",
    "mlm",
    "multi level marketing",
    "100% commission",
    "commission only",
    "daily payout",
    "quick money",
})

_SCAM_TITLE_KEYWORDS: frozenset[str] = frozenset({
    "home based agent",
    "online agent",
    "part time agent",
    "freelance recruiter",
    "earn daily",
    "reseller",
    "drop shipping",
    "dropshipping",
})

_EXEC_SENIORITY: frozenset[str] = frozenset({
    "director", "vp", "vice president", "executive", "c-level",
    "president", "partner", "principal", "staff", "distinguished",
})

_SUSPICIOUS_SALARY_THRESHOLD = 500_000  # >$500k for non-exec = suspicious


def _is_scam(posting: dict) -> bool:
    """Return True if posting shows scam signals — Addendum T."""
    desc = (posting.get("job_description") or "").lower()
    title = (posting.get("title") or "").lower()
    salary_max = posting.get("salary_max") or 0
    seniority = (posting.get("seniority_level") or "").lower()

    if any(kw in desc for kw in _SCAM_DESC_KEYWORDS):
        logger.info("Scam rejected (desc keyword): %s @ %s", title, posting.get("company_name"))
        return True

    if any(kw in title for kw in _SCAM_TITLE_KEYWORDS):
        logger.info("Scam rejected (title keyword): %s @ %s", title, posting.get("company_name"))
        return True

    if salary_max > _SUSPICIOUS_SALARY_THRESHOLD:
        if not any(s in seniority for s in _EXEC_SENIORITY):
            logger.info("Scam rejected (salary anomaly $%.0f): %s", salary_max, title)
            return True

    return False


def _deduplicate(records: list[dict]) -> list[dict]:
    """Remove cross-source duplicates by (title.lower, company.lower)."""
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for r in records:
        key = (
            r.get("title", "").lower().strip(),
            r.get("company_name", "").lower().strip(),
        )
        if key not in seen:
            seen.add(key)
            unique.append(r)
    removed = len(records) - len(unique)
    if removed:
        logger.info("Deduplication removed %d duplicate postings", removed)
    return unique


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def scrape_jobs(job_role: str, location: str) -> list[dict]:
    """
    Full pipeline: SERP → LinkedIn + Indeed (parallel) → scam filter → dedup → quality filter.
    Returns up to POSTINGS_CAP verified postings. PRD §7.1, §7.6, Addendum T.
    """
    _check_env()

    t0 = time.time()
    job_urls = _step1_serp(job_role, location)
    logger.info("Step 1: %d LinkedIn URLs in %.1fs", len(job_urls), time.time() - t0)

    t1 = time.time()
    postings = _step2_multi_source(job_urls[:POSTINGS_CAP], job_role, location)
    logger.info("Step 2: %d raw postings in %.1fs", len(postings), time.time() - t1)

    postings = _deduplicate(postings)
    return _quality_filter(postings)


# ---------------------------------------------------------------------------
# Step 1 — Location-aware SERP
# ---------------------------------------------------------------------------

def _poll_snapshot(snapshot_id: str, max_wait_s: int = SERP_POLL_MAX_S) -> dict:
    """Poll Bright Data snapshot API until ready or timeout. Returns data dict."""
    deadline = time.time() + max_wait_s
    interval = 5
    while time.time() < deadline:
        time.sleep(interval)
        try:
            poll = requests.get(
                f"https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}?format=json",
                headers=_headers(),
                timeout=15,
            )
            if poll.status_code == 200:
                data = poll.json()
                if isinstance(data, list) and data:
                    item = data[0] if isinstance(data[0], dict) else {}
                    if item.get("organic") or item.get("url"):
                        return item
                if isinstance(data, dict) and ("organic" in data or "url" in data):
                    return data
        except Exception as exc:
            logger.debug("Snapshot poll error: %s", exc)
    logger.warning("Snapshot %s not ready after %ds", snapshot_id, max_wait_s)
    return {}


_SERP_GL_MAP: dict[str, str] = {
    "malaysia": "my", "kuala lumpur": "my", "kl": "my", "penang": "my",
    "johor": "my", "selangor": "my",
    "singapore": "sg",
    "australia": "au", "sydney": "au", "melbourne": "au",
    "new zealand": "nz", "auckland": "nz",
    "india": "in", "bangalore": "in", "mumbai": "in", "delhi": "in",
    "philippines": "ph", "manila": "ph",
    "indonesia": "id", "jakarta": "id",
    "thailand": "th", "bangkok": "th",
    "united kingdom": "gb", "london": "gb",
    "germany": "de", "berlin": "de",
    "canada": "ca", "toronto": "ca",
    "united states": "us", "new york": "us", "san francisco": "us",
}


def _serp_gl(location: str) -> str:
    loc = location.lower()
    for kw, gl in _SERP_GL_MAP.items():
        if kw in loc:
            return gl
    return "us"


def _step1_serp(job_role: str, location: str) -> list[str]:
    """Bright Data SERP → LinkedIn job view URLs.

    Handles both sync responses (organic results) and async (snapshot_id).
    Quotes role for precision; location unquoted for recall.
    gl parameter geo-filters Google results to the correct country.
    """
    if location:
        keyword = f'"{job_role}" {location} site:linkedin.com/jobs/view'
        gl = _serp_gl(location)
    else:
        keyword = f'"{job_role}" jobs site:linkedin.com/jobs/view'
        gl = "us"

    payload = {
        "input": [
            {
                "url": "https://www.google.com/",
                "keyword": keyword,
                "language": "en",
                "gl": gl,
                "tbs": "qdr:m",
                "tbm": "",
                "uule": "",
                "nfpr": "",
                "index": "",
            }
        ]
    }
    try:
        resp = requests.post(
            _scrape_url(_DATASET_SERP),
            headers=_headers(),
            json=payload,
            timeout=SERP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        # Handle async response — Bright Data returns snapshot_id when not yet ready
        if isinstance(data, dict) and "snapshot_id" in data:
            logger.info("SERP async — polling snapshot %s", data["snapshot_id"])
            data = _poll_snapshot(data["snapshot_id"])

        if isinstance(data, list):
            data = data[0] if data and isinstance(data[0], dict) else {}

        organic = data.get("organic", []) if isinstance(data, dict) else []
        urls = [
            r["link"]
            for r in organic
            if "linkedin.com/jobs/view" in r.get("link", "")
        ]
        logger.info("SERP: %d LinkedIn URLs for '%s' in '%s'", len(urls), job_role, location)
        return urls
    except Exception as exc:
        logger.warning("SERP failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Step 2 — Multi-source parallel scrape
# ---------------------------------------------------------------------------

def _step2_multi_source(
    job_urls: list[str], job_role: str, location: str
) -> list[dict]:
    """Run LinkedIn Collect + Indeed direct API in parallel via ThreadPoolExecutor."""
    results: list[dict] = []

    futures_map: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        if job_urls and _DATASET_LINKEDIN:
            futures_map["linkedin"] = executor.submit(_fetch_linkedin, job_urls)
        if _DATASET_INDEED:
            futures_map["indeed"] = executor.submit(_fetch_indeed, job_role, location)

    for source, future in futures_map.items():
        try:
            results.extend(future.result())  # type: ignore[union-attr]
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
                "location": location,
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


# ---------------------------------------------------------------------------
# Quality + scam filter — Addendum T
# ---------------------------------------------------------------------------

def _quality_filter(records: list[dict]) -> list[dict]:
    """Keep postings that pass: description length + required fields + scam check."""
    qualified = []
    scam_count = 0
    for r in records:
        if len(r.get("job_description", "").strip()) < 200:
            continue
        if not r.get("title", "").strip():
            continue
        if not r.get("company_name", "").strip():
            continue
        if _is_scam(r):
            scam_count += 1
            r["is_verified"] = False
            continue
        r["is_verified"] = True
        qualified.append(r)

    qualified.sort(key=lambda r: len(r.get("job_description", "")), reverse=True)
    kept = qualified[:POSTINGS_CAP]
    logger.info(
        "Quality filter: %d/%d kept, %d scam-rejected",
        len(kept), len(records), scam_count,
    )
    return kept


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _normalise_linkedin(r: dict) -> dict:
    """Map LinkedIn Collect field names → standard posting dict (PRD §7.1)."""
    salary_min = _safe_float(r.get("salary_base_min"))
    salary_max = _safe_float(r.get("salary_base_max"))
    num_applicants = _safe_int(r.get("number_of_applicants"))

    remote_type = (r.get("remote_type") or "").lower()
    is_remote = remote_type in ("remote", "fully remote")
    seniority = (r.get("job_seniority_level") or "unknown").lower()

    return {
        "job_description": r.get("job_summary") or "",
        "title": r.get("job_title") or "",
        "company_name": r.get("company_name") or "",
        "url": r.get("url") or r.get("apply_link") or "",
        "apply_link": r.get("apply_link") or r.get("url") or "",
        "location": r.get("job_location") or "",
        "skills_listed": r.get("skills_listed") or [],
        "date_posted": r.get("job_posted_date") or "",
        "num_applicants": num_applicants,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": r.get("salary_currency") or "USD",
        "is_remote": is_remote,
        "remote_type": remote_type,
        "seniority_level": seniority,
        "employment_type": (r.get("job_employment_type") or "").lower(),
        "company_industry": r.get("company_industry") or "",
        "company_size": r.get("company_size") or "",
        "company_description": r.get("company_description") or "",
        "source": "linkedin",
        "is_verified": False,  # set by _quality_filter
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
    url = r.get("url") or r.get("job_url") or r.get("apply_link") or ""
    date_posted = r.get("job_posted_date") or r.get("date_posted") or r.get("posted_at") or ""

    return {
        "job_description": description,
        "title": r.get("job_title") or r.get("title") or "",
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
        "is_verified": False,  # set by _quality_filter
        "extracted_skills": [],
        "freshness_score": 0.5,
        "competition_score": 0.5,
        "dual_signal": {},
        "cross_source_confirmed": {},
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _scrape_url(dataset_id: str) -> str:
    return f"{_API_BASE}?dataset_id={dataset_id}&notify=false&include_errors=true"


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
