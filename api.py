"""
FastAPI backend — GapHunter Labor Market Intelligence Platform.
PRD §6, §8, Addendums A–G.
All endpoints return HTTP 200 with {"status": "ok"|"error"|...}.
Financial firewall (Addendum G): Demo Secret + IP rate limiter + circuit breaker.
"""

import asyncio
import json
import logging
import os
import re
import secrets as _secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import date as _date
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
from pydantic import BaseModel, EmailStr, field_validator

import auth as auth_module
from database import create_user, get_user_by_email, init_db, save_search, update_profile
from extractor import extract_all
from normalizer import precheck_role_input, validate_and_normalize
from pipeline import attach_evidence
from resume import parse_resume
from roadmap import (
    ROADMAP_CACHE,
    RoadmapEntry,
    RoadmapStatus,
    get_roadmap_status,
    init_roadmap_cache,
    prefetch_roadmaps,
)
from scoring import rank_gaps, rank_jobs
from security import validate_job_url
from signals import compute_cross_source, compute_dual_signal, enrich_postings

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App init
# ---------------------------------------------------------------------------

app = FastAPI(title="GapHunter API", version="2.0")

_ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:5500",
).split(",")

_MAX_REQUEST_BYTES = 2 * 1024 * 1024  # 2 MB hard cap — OOM bomb protection


class ContentLengthGuard:
    """Pure ASGI middleware defending against declared and chunked OOM payloads.

    Pure ASGI (not BaseHTTPMiddleware) so we own the receive callable directly —
    body is buffered into memory, limit enforced, then re-injected via a replacement
    receive callable passed to the downstream app. No double-read issues.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Stage 1: fast-fail on declared Content-Length
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        cl_raw = headers.get(b"content-length")
        if cl_raw:
            try:
                if int(cl_raw) > _MAX_REQUEST_BYTES:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                pass

        # Stage 2: buffer body chunks, abort if total exceeds limit
        total = 0
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                break
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > _MAX_REQUEST_BYTES:
                await self._reject(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        sent = False

        async def _safe_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, _safe_receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"detail": "Payload Too Large — maximum request size is 2 MB"},
            status_code=413,
        )
        await response(scope, receive, send)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _ALLOWED_ORIGINS],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    # X-Demo-Secret must be here or browser preflight blocks the header — PRD §23.6
    allow_headers=["Content-Type", "Authorization", "X-Demo-Secret"],
)
app.add_middleware(ContentLengthGuard)

_client: anthropic.AsyncAnthropic | None = None

_INVALID_QUERY_RESPONSE = {
    "status": "invalid_query",
    "message": "Please enter a valid job title (e.g. Data Analyst, Software Engineer).",
}
_RATE_LIMITED_RESPONSE = {
    "status": "rate_limited",
    "message": "Too many searches from your IP. Please wait 1 hour.",
}

_SCRAPE_TIMEOUT_S = int(os.environ.get("SCRAPE_TIMEOUT_S", "45"))
_FALLBACK_DIR = Path(__file__).parent / "fallback"
# Static fallback / demo mode — opt-in only. Set DEMO_MODE=1 in Render to enable.
# Without this flag, no hardcoded US data is ever served — callers get an empty result.
_DEMO_MODE = os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes")
_PROCESS_START = time.monotonic()
_STATIC_DEMO_PATH = Path(__file__).parent / "fallback" / "demo_state_data_analyst.json"
_static_demo_cache: dict | None = None


def _load_static_demo() -> dict:
    """Addendum N: load pre-captured demo state — zero LLM calls."""
    global _static_demo_cache
    if _static_demo_cache is None:
        if not _STATIC_DEMO_PATH.exists():
            raise FileNotFoundError(f"Static demo not found at {_STATIC_DEMO_PATH}")
        _static_demo_cache = json.loads(_STATIC_DEMO_PATH.read_text(encoding="utf-8"))
    return _static_demo_cache


def _ensure_static_cache() -> None:
    """Belt-and-suspenders: populate demo-static roadmap cache if missing.

    Startup seeds this at boot; this guard covers the case where startup seeding
    failed (e.g. file unreadable at init time) but succeeds later.
    """
    if "demo-static" not in ROADMAP_CACHE:
        try:
            roadmaps = _load_static_demo().get("roadmaps", {})
            if roadmaps:
                ROADMAP_CACHE["demo-static"] = {
                    skill: RoadmapEntry(status=RoadmapStatus.READY, roadmap=rm)
                    for skill, rm in roadmaps.items()
                }
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Addendum G — Demo-Day Financial Firewall
# ---------------------------------------------------------------------------

# Layer 1: Demo Secret
_DEMO_SECRET: str = ""  # set in startup()

async def _require_demo_secret(request: Request) -> None:
    """PRD §23.1. 403 on missing/wrong header. No-op in dev (secret not configured)."""
    if not _DEMO_SECRET:
        return
    header_val = request.headers.get("X-Demo-Secret", "")
    if not _secrets.compare_digest(header_val, _DEMO_SECRET):
        raise HTTPException(status_code=403)

# Layer 2: IP rate limiter (sliding window, 5 req/IP/hr)
_ip_windows: dict[str, deque] = defaultdict(deque)
_IP_LIMIT = 5
_IP_WINDOW_S = 3600

def _check_ip_rate(ip: str) -> bool:
    """PRD §23.2. Returns True if within limit. Prunes stale entries."""
    now = time.time()
    w = _ip_windows[ip]
    while w and now - w[0] > _IP_WINDOW_S:
        w.popleft()
    if len(w) >= _IP_LIMIT:
        return False
    w.append(now)
    return True

# Layer 3: Global circuit breaker (daily live search budget)
CIRCUIT_BREAKER_LIMIT = int(os.environ.get("CIRCUIT_BREAKER_LIMIT", "100"))

def _maybe_reset(state) -> None:
    """PRD §23.3. Daily reset on first request of a new calendar day."""
    today = _date.today()
    if state.reset_date != today:
        state.live_search_count = 0
        state.shadow_forced = False
        state.reset_date = today
        logger.info("Circuit breaker reset for %s", today)

def _tick_circuit_breaker(state) -> None:
    """PRD §23.3. Increment counter. Trip both flags independently at limit."""
    state.live_search_count += 1
    if state.live_search_count >= CIRCUIT_BREAKER_LIMIT:
        if not state.shadow_forced:
            state.shadow_forced = True
            logger.warning(
                "CIRCUIT BREAKER TRIPPED: %d live searches today. Shadow Mode forced.",
                state.live_search_count,
            )
        if not state.circuit_open:
            state.circuit_open = True
            logger.warning("CIRCUIT_OPEN: static demo state will serve all subsequent requests.")


@app.on_event("startup")
async def startup() -> None:
    global _client, _DEMO_SECRET
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — LLM calls will fail")
    _client = anthropic.AsyncAnthropic(api_key=api_key)

    jwt_secret = os.environ.get("JWT_SECRET", "")
    if not jwt_secret:
        logger.warning("JWT_SECRET not set — using insecure fallback")
        jwt_secret = "dev-insecure-secret-change-me"
    auth_module.init_auth(jwt_secret)

    _DEMO_SECRET = os.environ.get("DEMO_SECRET", "")
    if not _DEMO_SECRET:
        logger.warning("DEMO_SECRET not set — Layer 1 firewall disabled (dev mode)")

    # Circuit breaker initial state (PRD §23.3)
    app.state.live_search_count = 0
    app.state.shadow_forced = False
    app.state.circuit_open = False
    app.state.fallback_ready = False
    app.state.reset_date = _date.today()

    # Pre-verify Shadow Mode fallback file is accessible — sets fallback_ready=True at startup
    # so /health Go/No-Go gate passes without needing a live scrape to fail first.
    _load_fallback("data analyst")

    # Addendum O: pre-seed demo-static roadmap cache so polling resolves instantly
    # when circuit breaker is open during recording (session_id == "demo-static").
    if _STATIC_DEMO_PATH.exists():
        try:
            _static_roadmaps = json.loads(
                _STATIC_DEMO_PATH.read_text(encoding="utf-8")
            ).get("roadmaps", {})
            if _static_roadmaps:
                ROADMAP_CACHE["demo-static"] = {
                    skill: RoadmapEntry(status=RoadmapStatus.READY, roadmap=rm)
                    for skill, rm in _static_roadmaps.items()
                }
                logger.info(
                    "Demo-static roadmap cache seeded: %d skills", len(_static_roadmaps)
                )
        except Exception as _exc:
            logger.warning("Demo-static roadmap seed failed: %s", _exc)

    init_db()
    logger.info("GapHunter API ready")


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------

def _get_current_user(request: Request) -> dict | None:
    token = request.cookies.get("access_token")
    if not token:
        return None
    return auth_module.decode_token(token)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host or "unknown")


# ---------------------------------------------------------------------------
# Shadow Mode scraping (Addendum C)
# ---------------------------------------------------------------------------

def _load_fallback(role: str) -> list[dict]:
    slug = re.sub(r"[^a-z0-9]+", "_", role.lower()).strip("_")
    candidates = [
        _FALLBACK_DIR / f"fallback_payload_{slug}.json",
        _FALLBACK_DIR / "fallback_payload_data_analyst.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                with path.open() as f:
                    data = json.load(f)
                logger.info("Shadow Mode: loaded fallback from %s", path.name)
                result = data if isinstance(data, list) else []
                if result:
                    app.state.fallback_ready = True
                return result
            except Exception as exc:
                logger.warning("Fallback load failed: %s", exc)
    return []


# ---------------------------------------------------------------------------
# Global job board web extraction — Addendum Y
# Fallback when LinkedIn+Indeed APIs return 0: SERP any board → Web Unlocker → Claude extract
# Covers: Jobstreet (MY/SG/PH), Jobsdb (MY/SG/HK), Seek (AU/NZ), Naukri (IN),
#         Reed (UK), Wobb (MY), Monster, Glassdoor, company career pages
# ---------------------------------------------------------------------------

_JOB_BOARD_PATTERNS: tuple[str, ...] = (
    # Domain-level match — SERP URLs use many path formats, don't over-constrain
    "linkedin.com/jobs/",
    "jobstreet.com",
    "jobsdb.com",
    "seek.com.au",
    "seek.co.nz",
    "naukri.com",
    "reed.co.uk",
    "totaljobs.com",
    "cv-library.co.uk",
    "wobb.my",
    "hiredly.com",
    "jobscentral.com.sg",
    "mycareersfuture.gov.sg",
    "kalibrr.com",
    "glints.com",
    "glassdoor.com",
    "monster.com",
    "indeed.com",       # any Indeed URL — path varies by country/device
    "stepstone.de",
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "bamboohr.com",
    # Generic path patterns
    "/jobs/view/",
    "/job-detail/",
    "/job/",
    "/careers/jobs/",
    "/en/jobs/",
)


def _detect_job_source(url: str) -> str:
    if "linkedin.com" in url:
        return "linkedin"
    if "jobstreet.com" in url:
        return "jobstreet"
    if "jobsdb.com" in url:
        return "jobsdb"
    if "seek.com.au" in url:
        return "seek"
    if "naukri.com" in url:
        return "naukri"
    if "reed.co.uk" in url:
        return "reed"
    if "wobb.my" in url:
        return "wobb"
    if "indeed.com" in url:
        return "indeed"
    if "glassdoor.com" in url:
        return "glassdoor"
    return "web"


_GL_MAP: dict[str, str] = {
    "malaysia": "my", "kuala lumpur": "my", "penang": "my", "johor": "my",
    "singapore": "sg",
    "australia": "au", "sydney": "au", "melbourne": "au", "brisbane": "au",
    "new zealand": "nz", "auckland": "nz",
    "india": "in", "bangalore": "in", "mumbai": "in", "delhi": "in", "chennai": "in",
    "philippines": "ph", "manila": "ph",
    "indonesia": "id", "jakarta": "id",
    "thailand": "th", "bangkok": "th",
    "united kingdom": "uk", "london": "uk", "manchester": "uk",
    "germany": "de", "berlin": "de",
    "canada": "ca", "toronto": "ca", "vancouver": "ca",
    "united states": "us", "new york": "us", "san francisco": "us",
}


def _gl_for_location(location: str) -> str:
    loc_lower = location.lower()
    for kw, gl in _GL_MAP.items():
        if kw in loc_lower:
            return gl
    return "us"


def _serp_broad_jobs_sync(role: str, location: str) -> list[str]:
    """SERP for real job postings on ANY public job board — global coverage."""
    import requests as _req

    token = os.environ.get("BRIGHTDATA_API_TOKEN", "")
    dataset = os.environ.get("BRIGHTDATA_DATASET_ID", "")
    if not token or not dataset:
        return []

    if location:
        # Unquoted location — double-quoting is too strict for regional SERP
        keyword = f'"{role}" jobs {location}'
        gl = _gl_for_location(location)
    else:
        keyword = f'"{role}" remote jobs'
        gl = "us"

    payload = {
        "input": [{
            "url": f"https://www.google.com/search?gl={gl}",
            "keyword": keyword,
            "language": "en",
            "tbs": "qdr:m",
            "tbm": "",
            "uule": "",
            "nfpr": "",
            "index": "",
        }]
    }
    try:
        resp = _req.post(
            f"https://api.brightdata.com/datasets/v3/scrape"
            f"?dataset_id={dataset}&notify=false&include_errors=true",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()

        # Bright Data Dataset API can return async (snapshot_id) or sync results.
        # Sync: dict with "organic" key. Async: dict with "snapshot_id".
        # List: direct array of result objects (deliver_to=api_response).
        if isinstance(data, dict) and "snapshot_id" in data:
            # Async response — poll for up to 15s
            snap_id = data["snapshot_id"]
            for _ in range(5):
                import time as _time
                _time.sleep(3)
                poll = _req.get(
                    f"https://api.brightdata.com/datasets/v3/snapshot/{snap_id}?format=json",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                if poll.status_code == 200:
                    poll_data = poll.json()
                    if isinstance(poll_data, list) and poll_data:
                        data = poll_data[0] if isinstance(poll_data[0], dict) else {}
                        break
                    elif isinstance(poll_data, dict) and "organic" in poll_data:
                        data = poll_data
                        break
            else:
                logger.warning("Broad SERP snapshot not ready after 15s — skipping")
                return []

        # Handle list-wrapped response
        if isinstance(data, list):
            data = data[0] if data and isinstance(data[0], dict) else {}

        organic = data.get("organic") or [] if isinstance(data, dict) else []
        urls = [
            r.get("link", "")
            for r in organic
            if any(pat in r.get("link", "") for pat in _JOB_BOARD_PATTERNS)
        ]
        logger.info("Broad SERP: %d job URLs for '%s' in '%s'", len(urls), role, location)
        return urls[:8]
    except Exception as exc:
        logger.warning("Broad SERP failed: %s", exc)
        return []


async def _extract_job_from_page(url: str, page_text: str) -> dict | None:
    """Claude Haiku extracts a normalized job posting from any job page's text."""
    if len(page_text) < 200:
        return None
    prompt = (
        f"Extract job posting data from this page text. Source URL: {url}\n\n"
        f"{page_text}\n\n"
        "Return JSON only with these exact fields (null if not found):\n"
        "job_description (string 200+ chars), title (string), company_name (string), "
        "location (string), "
        "salary_min (number — ANNUAL salary minimum, convert monthly×12 if needed, null if not stated), "
        "salary_max (number — ANNUAL salary maximum, convert monthly×12 if needed, null if not stated), "
        "salary_currency (ISO currency code matching the job location — MYR for Malaysia, SGD for Singapore, "
        "AUD for Australia, INR for India, GBP for UK, USD for US, etc.), "
        "seniority_level (entry/mid/senior/unknown), "
        "employment_type (full-time/part-time/contract), is_remote (boolean), "
        "remote_type (remote/hybrid/on-site), skills_listed (array of strings), "
        "date_posted (YYYY-MM-DD or null).\n"
        'Return the JSON object. Return {"not_a_job": true} if this is not a job posting.'
    )
    try:
        async with asyncio.timeout(12):
            resp = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=500,
                system="Extract job posting data. Return valid JSON only.",
                messages=[{"role": "user", "content": prompt}],
            )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        r = json.loads(text)
        if not isinstance(r, dict) or r.get("not_a_job") or not r.get("title") or not r.get("company_name"):
            return None
        desc = r.get("job_description") or ""
        if len(desc) < 200:
            return None
        return {
            "job_description": desc,
            "title": r.get("title") or "",
            "company_name": r.get("company_name") or "",
            "url": url,
            "apply_link": url,
            "location": r.get("location") or "",
            "skills_listed": r.get("skills_listed") or [],
            "date_posted": r.get("date_posted") or "",
            "num_applicants": None,
            "salary_min": float(r.get("salary_min") or 0),
            "salary_max": float(r.get("salary_max") or 0),
            "salary_currency": r.get("salary_currency") or "USD",
            "is_remote": bool(r.get("is_remote", False)),
            "remote_type": (r.get("remote_type") or "").lower(),
            "seniority_level": (r.get("seniority_level") or "unknown").lower(),
            "employment_type": (r.get("employment_type") or "full-time").lower(),
            "company_industry": "",
            "company_size": "",
            "company_description": "",
            "source": _detect_job_source(url),
            "is_verified": False,
            "extracted_skills": [],
            "freshness_score": 0.5,
            "competition_score": 0.5,
            "dual_signal": {},
            "cross_source_confirmed": {},
        }
    except Exception as exc:
        logger.warning("Job extraction failed for %s: %s", url, exc)
        return None


async def _infer_role_from_skills(skills_str: str) -> str | None:
    """Backend fallback: infer job title from skills when role field is empty."""
    if not _client or not skills_str.strip():
        return None
    skills_sample = ", ".join(s.strip() for s in skills_str.split(",") if s.strip())[:300]
    try:
        async with asyncio.timeout(8):
            resp = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                system="You are a career advisor. Return one job title only — no explanation.",
                messages=[{
                    "role": "user",
                    "content": (
                        f"Given these skills: {skills_sample}\n"
                        "What is the single most likely job title this person should search for? "
                        "Return the job title only, nothing else."
                    ),
                }],
            )
        title = resp.content[0].text.strip().strip('"').strip("'")
        return title if title else None
    except Exception as exc:
        logger.warning("_infer_role_from_skills failed: %s", exc)
        return None


_PLAIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _plain_get_sync(url: str, timeout: int = 12) -> str:
    """Plain requests.get — no Web Unlocker. Works for bot-friendly pages (DDG, Indeed, Seek)."""
    import requests as _req
    try:
        resp = _req.get(url, headers=_PLAIN_HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200 and len(resp.text) >= 300:
            return resp.text
    except Exception as exc:
        logger.debug("Plain GET failed for %s: %s", url, exc)
    return ""


async def _fetch_best_effort(url: str) -> str:
    """Try plain GET first (free, fast), fall back to Web Unlocker for bot-protected pages."""
    text = await asyncio.to_thread(_plain_get_sync, url)
    if text:
        logger.info("Plain GET: %d chars from %s", len(text), url)
        return text
    return await _fetch_with_unlocker(url)


async def _ddg_job_search(role: str, location: str) -> tuple[list[str], str]:
    """
    Fetch DuckDuckGo HTML with plain requests (no Web Unlocker, no API key).
    Returns (job_board_urls, raw_html_text) — html_text used by caller for snippet extraction.
    DuckDuckGo HTML endpoint is publicly accessible and bot-friendly.
    """
    from urllib.parse import quote_plus, unquote
    query = f'{role} jobs {location} 2025' if location else f'{role} remote jobs 2025'
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    html = await asyncio.to_thread(_plain_get_sync, url, 15)
    if not html:
        logger.warning("DDG plain fetch returned empty")
        return [], ""

    seen: set[str] = set()
    job_urls: list[str] = []
    # DDG direct hrefs
    for raw in re.findall(r'href=["\']?(https?://[^"\'<>\s]+)', html):
        u = raw.split("&")[0]
        if "duckduckgo.com" in u:
            continue
        if any(pat in u for pat in _JOB_BOARD_PATTERNS) and u not in seen:
            seen.add(u)
            job_urls.append(u)
    # DDG redirect links: /l/?uddg=ENCODED
    for encoded in re.findall(r'uddg=([^&"\'<>\s]+)', html):
        u = unquote(encoded).split("&")[0]
        if any(pat in u for pat in _JOB_BOARD_PATTERNS) and u not in seen:
            seen.add(u)
            job_urls.append(u)

    logger.info("DDG plain: %d job URLs for '%s' in '%s'", len(job_urls), role, location)
    return job_urls[:8], html


async def _find_jobs_via_web(role: str, location: str) -> list[dict]:
    """
    Multi-tier job discovery — no hardcoded fallback, all real postings.
    Tier 1: Bright Data Dataset SERP → individual pages via Web Unlocker
    Tier 2: DDG plain HTTP SERP → individual pages via plain GET + Web Unlocker
    Tier 3: Direct job board search pages (location-mapped) via plain GET + Unlocker
    Tier 4: Extract listings from DDG snippet text when page fetches all fail
    """
    # Tier 1: Dataset API SERP
    job_urls: list[str] = await asyncio.to_thread(_serp_broad_jobs_sync, role, location)
    ddg_html = ""

    # Tier 2: DDG plain requests (no Web Unlocker needed)
    if not job_urls:
        logger.info("Dataset SERP: 0 — trying DDG plain")
        job_urls, ddg_html = await _ddg_job_search(role, location)

    # Tier 3: Direct job board search pages
    if not job_urls:
        logger.info("DDG: 0 — trying direct board URLs")
        job_urls = _direct_board_urls(role, location)

    # Fetch each page: plain GET first, Web Unlocker as fallback
    async def _fetch_safe(url: str) -> tuple[str, str]:
        try:
            html = await _fetch_best_effort(url)
            return url, _html_to_text(html, max_chars=4000) if html else ""
        except Exception:
            return url, ""

    fetch_results = await asyncio.gather(*[_fetch_safe(u) for u in job_urls[:6]])
    page_texts = [(url, text) for url, text in fetch_results if text]

    # Try extracting individual job pages first
    if page_texts:
        extracted = await asyncio.gather(*[
            _extract_job_from_page(url, text) for url, text in page_texts
        ])
        jobs = [j for j in extracted if j is not None]
        if jobs:
            logger.info("Page extraction: %d jobs for '%s'", len(jobs), role)
            return jobs

        # Individual page extraction failed — treat pages as search results listings
        all_listing_jobs: list[dict] = []
        for url, text in page_texts[:2]:
            source = _detect_job_source(url)
            listing_jobs = await _extract_jobs_from_search_page(url, text, role, location, source)
            all_listing_jobs.extend(listing_jobs)
        if all_listing_jobs:
            logger.info("Listing extraction: %d jobs for '%s'", len(all_listing_jobs), role)
            return all_listing_jobs[:8]

    # Tier 4: Last resort — extract from DDG snippet text if available
    if ddg_html:
        logger.info("All page fetches failed — extracting from DDG snippets")
        snippet_jobs = await _extract_jobs_from_search_page(
            f"https://html.duckduckgo.com/html/?q={role}+jobs+{location}",
            _html_to_text(ddg_html, max_chars=8000),
            role, location, "search"
        )
        if snippet_jobs:
            return snippet_jobs[:8]

    logger.warning("All tiers exhausted for '%s' in '%s'", role, location)
    return []


async def _scrape_with_fallback(role: str, location: str) -> tuple[list[dict], str]:
    """
    Returns (postings, data_source) where data_source is:
    - "live":          Bright Data LinkedIn+Indeed structured APIs returned results
    - "web_extracted": SERP + Web Unlocker found real jobs from regional job boards
    - "fallback":      static JSON (last resort — circuit breaker / all APIs down)
    """
    from scraper import scrape_jobs

    # Try 1: Structured LinkedIn + Indeed APIs
    try:
        postings = await asyncio.wait_for(
            asyncio.to_thread(scrape_jobs, role, location),
            timeout=_SCRAPE_TIMEOUT_S,
        )
        if postings:
            return postings, "live"
        logger.warning("Scrape returned 0 — trying web extraction")
    except asyncio.TimeoutError:
        logger.warning("Scrape timed out after %ds — trying web extraction", _SCRAPE_TIMEOUT_S)
    except Exception as exc:
        logger.warning("Scrape error: %s — trying web extraction", exc)

    # Try 2: Multi-tier web extraction (DDG + direct boards + Claude extraction)
    # Timeout raised to 90s — 4 tiers each take up to ~25s, parallelised internally
    try:
        async with asyncio.timeout(90):
            web_jobs = await _find_jobs_via_web(role, location)
        if web_jobs:
            return web_jobs, "web_extracted"
    except asyncio.TimeoutError:
        logger.warning("Web extraction timed out after 90s")
    except Exception as exc:
        logger.warning("Web extraction error: %s", exc)

    # Try 3: Static fallback — only when DEMO_MODE=1 is explicitly set
    if _DEMO_MODE:
        logger.warning("All sources failed — DEMO_MODE active, serving static fallback")
        return _load_fallback(role), "fallback"

    logger.warning("All sources failed — no hardcoded fallback (DEMO_MODE not set)")
    return [], "no_results"


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: str
    password: str

    @field_validator("password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str


class SearchPreferences(BaseModel):
    remote: bool = False
    seniority: str = "unknown"
    employment_type: str = ""
    salary_min: float = 0.0
    days_posted: int = 30
    company_size: str = ""


class SearchRequest(BaseModel):
    role: str
    location: str = ""
    skills: str = ""
    preferences: SearchPreferences = SearchPreferences()
    session_id: str | None = None  # client hint for tracking — never used as a cache key directly


class AnalyseRequest(BaseModel):
    job_url: str
    session_id: str
    job_title: str | None = None
    company: str | None = None
    location: str | None = None
    seniority: str | None = None
    salary: str | None = None
    skills_match: list[str] | None = None


class HRRequest(BaseModel):
    company_name: str
    role: str
    location: str = ""


class CompanyRequest(BaseModel):
    company_name: str
    session_id: str


class SalaryRequest(BaseModel):
    role: str
    location: str = ""


class TailormanRequest(BaseModel):
    user_skills: str               # comma-separated skills from CV parse
    job_title: str
    job_company: str = ""
    job_location: str = ""
    highlight_skills: list[str] = []   # from gap analysis — skills user already has
    gap_skills: list[str] = []         # from gap analysis — skills user is missing
    seniority: str = ""


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.post("/api/auth/register")
async def register(req: RegisterRequest) -> dict:
    hashed = auth_module.hash_password(req.password)
    user_id = create_user(req.email, hashed)
    if user_id is None:
        return {"status": "error", "message": "Email already registered"}
    return {"status": "ok", "user_id": user_id}


@app.post("/api/auth/login")
async def login(req: LoginRequest, request: Request, response: Response) -> dict:
    ip = _client_ip(request)
    if auth_module.is_rate_limited(ip):
        return {"status": "error", "message": "Too many attempts. Try again in 5 minutes."}

    user = get_user_by_email(req.email)
    if not user or not auth_module.verify_password(req.password, user["hashed_password"]):
        return {"status": "error", "message": "Invalid credentials"}

    access_token = auth_module.create_access_token(user["id"])
    refresh_token = auth_module.create_refresh_token(user["id"])

    response.set_cookie("access_token", access_token, httponly=True, secure=True, samesite="lax",
                        max_age=auth_module.ACCESS_TOKEN_EXPIRE_S)
    response.set_cookie("refresh_token", refresh_token, httponly=True, secure=True, samesite="lax",
                        max_age=auth_module.REFRESH_TOKEN_EXPIRE_S)
    return {"status": "ok"}


@app.post("/api/auth/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Resume upload (F1)
# ---------------------------------------------------------------------------

_DEMO_RESUME_SKILLS = ["Excel", "Tableau", "SQL", "Python", "PowerPoint", "Data Visualization"]

@app.post("/api/resume", dependencies=[Depends(_require_demo_secret)])
async def upload_resume(request: Request, file: UploadFile = File(...)) -> dict:
    """Addendum E: 7-layer defence. Always HTTP 200."""
    # Addendum N extension: circuit open → zero-LLM static demo skills
    if getattr(request.app.state, "circuit_open", False):
        logger.info("CIRCUIT_OPEN: serving static demo skills — zero LLM calls")
        return {"status": "ok", "skills": _DEMO_RESUME_SKILLS, "experience_years": 3, "seniority": "mid"}

    file_bytes = await file.read()
    content_type = file.content_type or ""

    parsed = await parse_resume(_client, file_bytes, content_type)
    if not parsed:
        return {"status": "parse_failed", "message": "Could not extract skills from your CV. Try manual entry."}

    return {
        "status": "ok",
        "skills": parsed["skills"],
        "experience_years": parsed["experience_years"],
        "seniority": parsed["seniority"],
    }


# ---------------------------------------------------------------------------
# Core search + ranking (F2)
# ---------------------------------------------------------------------------

@app.post("/api/search", dependencies=[Depends(_require_demo_secret)])
async def search(req: SearchRequest, request: Request, background_tasks: BackgroundTasks) -> dict:
    """
    Full pipeline: validate → scrape (Shadow Mode) → enrich → extract
    → rank gaps → rank jobs → fire-and-forget roadmap prefetch.
    Firewall: Demo Secret (Layer 1) + IP rate limit (Layer 2) + circuit breaker (Layer 3).
    """
    # Addendum N: zero-token static fallback when circuit is open
    if getattr(request.app.state, "circuit_open", False):
        logger.warning("CIRCUIT_OPEN: serving static demo state — zero LLM calls")
        _ensure_static_cache()
        return JSONResponse(content=_load_static_demo())

    # Layer 2: IP rate limit (Addendum G §23.2) — Fracture 4 fix.
    # Authenticated requests (carrying a valid X-Demo-Secret) are exempt from the 5 req/hr cap.
    # Rationale: the secret IS the whitelist. Judges behind a shared VPN/NAT would otherwise
    # exhaust the global 5/hr window. Unauthenticated traffic is already blocked at Layer 1 (403),
    # so this branch fires only if Layer 1 dependency is relaxed in future — defense-in-depth.
    _secret_header = request.headers.get("x-demo-secret", "")
    _is_authenticated = bool(
        _DEMO_SECRET and _secrets.compare_digest(_secret_header, _DEMO_SECRET)
    )
    if not _is_authenticated:
        ip = _client_ip(request)
        if not _check_ip_rate(ip):
            return _RATE_LIMITED_RESPONSE

    # Pre-Gate: if role is empty but skills were sent (CV uploaded), infer role
    role_input = req.role.strip()
    if not precheck_role_input(role_input) and req.skills.strip():
        inferred = await _infer_role_from_skills(req.skills)
        if inferred:
            logger.info("Role inferred from skills: '%s'", inferred)
            role_input = inferred
        else:
            return _INVALID_QUERY_RESPONSE

    # Gate 0: pure Python (Addendum F)
    if not precheck_role_input(role_input):
        return _INVALID_QUERY_RESPONSE

    # Layer 3: Daily reset + shadow forced early-exit (Fracture 3 fix)
    # Must run before Gate 1 so a downed Bright Data stack costs zero LLM tokens.
    _maybe_reset(request.app.state)
    if request.app.state.shadow_forced:
        if _DEMO_MODE:
            logger.info("SHADOW_FORCED + DEMO_MODE: serving static demo state")
            _ensure_static_cache()
            return JSONResponse(content=_load_static_demo())
        else:
            logger.info("SHADOW_FORCED: search budget exhausted, DEMO_MODE not set — returning error")
            return {"status": "error", "message": "Live search unavailable right now. Please try again later."}

    # Gate 1: Claude Haiku normalisation (Addendum F)
    # Gate 0 already blocked garbage — if Gate 1 says invalid, log and proceed
    # rather than hard-blocking legitimate but unusual job titles.
    validation = await validate_and_normalize(_client, role_input)
    if validation is None:
        titles = [role_input]  # degrade gracefully on timeout
    elif not validation["is_valid_role"]:
        logger.info("Gate 1 flagged '%s' as non-standard — proceeding (Gate 0 passed)", role_input)
        titles = [role_input]
    else:
        titles = validation["canonical_titles"] or [role_input]

    primary_title = titles[0]

    # Layer 3: Tick circuit breaker (only live path reaches here)
    _tick_circuit_breaker(request.app.state)
    postings, data_source = await _scrape_with_fallback(primary_title, req.location)

    if not postings:
        return {"status": "error", "message": "No job postings found. Try a different role or location."}

    # Phase 1 signals (no skills yet)
    enrich_postings(postings)

    # Extract skills — Claude Haiku, semaphore-capped (Addendum A)
    job_descriptions = [p["job_description"] for p in postings if p["job_description"]]
    all_skills_lists = await extract_all(_client, job_descriptions)

    # Attach extracted skills back to postings
    for posting, skills in zip(postings, all_skills_lists):
        posting["extracted_skills"] = skills

    # Phase 2 signals (skills now available)
    compute_dual_signal(postings)
    compute_cross_source(postings)

    # Rank gaps using weighted demand score (replaces Counter())
    user_skills_raw = req.skills or ""
    gaps = rank_gaps(all_skills_lists, user_skills_raw, postings, top_n=5)

    # Attach evidence URLs to gaps
    gap_tuples = [(g["skill"], g["score"]) for g in gaps]
    evidence = attach_evidence(gap_tuples, postings, all_skills_lists)
    # Rename legacy "count" field → "demand_score" (was Counter(), now weighted float)
    for item in evidence:
        item["demand_score"] = round(item.pop("count", 0), 4)

    # Rank jobs by personalised relevance
    user_prefs = {
        "skills": [s.strip() for s in user_skills_raw.split(",") if s.strip()],
        "seniority": req.preferences.seniority,
        "remote": req.preferences.remote,
        "salary_min": req.preferences.salary_min,
    }
    ranked_jobs = rank_jobs(list(postings), user_prefs)

    # Build job cards
    # Patch 4: Cache poison guard — "demo-static" is a reserved partition owned by the golden path.
    # A client sending session_id="demo-static" on the live path would, if ever used as a cache key,
    # overwrite startup-seeded roadmap entries with pending stubs, breaking Addendum O.
    # Force a fresh UUID regardless; log any attempt to claim the reserved ID.
    if req.session_id == "demo-static":
        logger.warning("POISON_GUARD: client claimed reserved session_id='demo-static' on live path — issuing new UUID")
    session_id = str(uuid.uuid4())
    jobs_out = [_format_job(j) for j in ranked_jobs[:10]]

    # Persist search history (optional — no user required)
    current_user = _get_current_user(request)
    user_id = current_user["sub"] if current_user else None
    background_tasks.add_task(save_search, session_id, req.role, req.location, evidence, user_id)

    # Addendum D: fire-and-forget roadmap pre-fetch
    gap_skills = [g["skill"] for g in gaps]
    # F5: pass demand scores so roadmap why_it_matters is market-grounded
    gap_score_map = {g["skill"]: round(g.get("demand_score", 0.0), 4) for g in evidence}
    init_roadmap_cache(session_id, gap_skills)
    asyncio.create_task(
        prefetch_roadmaps(_client, session_id, gap_skills, job_descriptions, gap_score_map)
    )

    return {
        "status": "ok",
        "session_id": session_id,
        "total_postings": len(postings),
        "titles_searched": titles,
        "jobs": jobs_out,
        "gaps": evidence,
        "data_source": data_source,
        "location_searched": req.location,
    }


_CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$", "MYR": "RM ", "SGD": "S$", "AUD": "A$", "NZD": "NZ$",
    "GBP": "£", "EUR": "€", "INR": "₹", "PHP": "₱", "IDR": "Rp ",
    "THB": "฿", "HKD": "HK$", "CAD": "C$", "JPY": "¥", "CNY": "¥",
    "KRW": "₩", "TWD": "NT$", "VND": "₫",
}


def _format_job(job: dict) -> dict:
    url = job.get("apply_link") or job.get("url") or ""
    safe_url = url if validate_job_url(url) else ""

    salary_min = job.get("salary_min", 0) or 0
    salary_max = job.get("salary_max", 0) or 0
    currency = (job.get("salary_currency") or "USD").upper()
    symbol = _CURRENCY_SYMBOLS.get(currency, currency + " ")
    salary_display = (
        f"{symbol}{int(salary_min):,}–{symbol}{int(salary_max):,}/yr"
        if salary_min > 0 and salary_max > 0
        else "Not disclosed"
    )

    return {
        "title": job.get("title", ""),
        "company": job.get("company_name", ""),
        "location": job.get("location", ""),
        "url": safe_url,
        "relevance_pct": round(job.get("relevance_score", 0) * 100),
        "freshness_score": job.get("freshness_score", 0.5),
        "competition_score": job.get("competition_score", 0.5),
        "is_remote": job.get("is_remote", False),
        "seniority": job.get("seniority_level", "unknown"),
        "salary": salary_display,
        "source": job.get("source", ""),
        "date_posted": job.get("date_posted", ""),
        "skills_match": job.get("extracted_skills", [])[:8],
        "is_verified": job.get("is_verified", True),
        "company_description": (job.get("company_description") or "")[:400],
        "company_size": job.get("company_size", ""),
        "company_industry": job.get("company_industry", ""),
    }


# ---------------------------------------------------------------------------
# Roadmap polling (F4) — Addendum D
# ---------------------------------------------------------------------------

@app.get("/api/roadmap/{skill}")
async def get_roadmap(skill: str, session_id: str) -> dict:
    """Always HTTP 200. Frontend polls until status == 'ready'."""
    return get_roadmap_status(session_id, skill)


# ---------------------------------------------------------------------------
# Bright Data Web Unlocker — Addendum Q
# ---------------------------------------------------------------------------

_UNLOCKER_ENDPOINT = "https://api.brightdata.com/request"
_UNLOCKER_ZONE = os.environ.get("BRIGHTDATA_UNLOCKER_ZONE", "unlocker")
_UNLOCKER_TIMEOUT_S = 15


def _unlocker_fetch_sync(url: str) -> str:
    """Synchronous Web Unlocker fetch — run via asyncio.to_thread."""
    import requests as _req  # lazy: requests present (scraper.py dep)
    token = os.environ.get("BRIGHTDATA_API_TOKEN", "")
    if not token:
        return ""
    resp = _req.post(
        _UNLOCKER_ENDPOINT,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"url": url, "zone": _UNLOCKER_ZONE, "format": "raw"},
        timeout=_UNLOCKER_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.text


async def _fetch_with_unlocker(url: str) -> str:
    """Addendum Q: Bright Data Web Unlocker — real job page HTML. Silent fallback."""
    try:
        async with asyncio.timeout(_UNLOCKER_TIMEOUT_S + 3):
            text = await asyncio.to_thread(_unlocker_fetch_sync, url)
        if len(text) >= 200:
            logger.info("Web Unlocker: %d chars from %s", len(text), url)
            return text
        logger.warning("Web Unlocker: too short (%d chars), falling back", len(text))
    except Exception as exc:
        logger.warning("Web Unlocker failed for %s: %s — metadata fallback", url, exc)
    return ""


def _html_to_text(html: str, max_chars: int = 3000) -> str:
    """Strip HTML tags, condense whitespace for Claude prompt injection."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


async def _serp_via_unlocker(role: str, location: str) -> list[str]:
    """
    DuckDuckGo HTML SERP via Web Unlocker.
    DuckDuckGo's HTML endpoint returns plain, JS-free HTML with direct hrefs —
    unlike Google which is JS-heavy and often serves bot-detection pages.
    Only needs BRIGHTDATA_API_TOKEN + BRIGHTDATA_UNLOCKER_ZONE.
    """
    from urllib.parse import quote_plus, unquote

    query = f'"{role}" jobs {location}' if location else f'"{role}" remote jobs'
    # DuckDuckGo HTML endpoint: returns plain HTML, no JavaScript required
    search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

    try:
        html = await _fetch_with_unlocker(search_url)
        if not html or len(html) < 500:
            logger.warning("DDG SERP: too little HTML (%d chars)", len(html or ""))
            return []

        # DDG HTML has direct hrefs and /l/?uddg=ENCODED_URL redirect links
        seen: set[str] = set()
        job_urls: list[str] = []

        for raw in re.findall(r'href=["\']?(https?://[^"\'<>\s]+)', html):
            url = unquote(raw).split("&")[0]
            if any(skip in url for skip in ("duckduckgo.com", "google.com", "gstatic.com")):
                continue
            if any(pat in url for pat in _JOB_BOARD_PATTERNS) and url not in seen:
                seen.add(url)
                job_urls.append(url)

        # DDG also wraps results as /l/?uddg=PERCENT_ENCODED_URL
        for encoded in re.findall(r'uddg=([^&"\'<>\s]+)', html):
            url = unquote(encoded).split("&")[0]
            if any(pat in url for pat in _JOB_BOARD_PATTERNS) and url not in seen:
                seen.add(url)
                job_urls.append(url)

        logger.info("DDG SERP: %d job URLs for '%s' in '%s'", len(job_urls), role, location)
        return job_urls[:8]
    except Exception as exc:
        logger.warning("DDG SERP failed: %s", exc)
        return []


def _direct_board_urls(role: str, location: str) -> list[str]:
    """
    Build job board search page URLs directly from role + location — no SERP needed.
    Location keyword → regional boards → constructed search URLs.
    """
    from urllib.parse import quote_plus
    role_slug = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")
    role_q = quote_plus(role)
    loc_q = quote_plus(location)
    loc = location.lower()

    if any(k in loc for k in ["malaysia", "kuala lumpur", " kl", "penang", "johor", "selangor", "petaling"]):
        return [
            f"https://www.jobstreet.com.my/en/job-search/{role_slug}-jobs/in-malaysia/",
            f"https://malaysia.indeed.com/jobs?q={role_q}&l={loc_q}",
            f"https://www.wobb.my/jobs?position={role_q}",
        ]
    if any(k in loc for k in ["singapore", " sg ", "sg,"]):
        return [
            f"https://www.jobstreet.com.sg/en/job-search/{role_slug}-jobs/in-singapore/",
            f"https://sg.indeed.com/jobs?q={role_q}",
            f"https://www.mycareersfuture.gov.sg/search?search={role_q}&sortBy=new_posting_date",
        ]
    if any(k in loc for k in ["australia", "sydney", "melbourne", "brisbane", "perth", "adelaide"]):
        return [
            f"https://www.seek.com.au/{role_slug}-jobs",
            f"https://au.indeed.com/jobs?q={role_q}&l={loc_q}",
        ]
    if any(k in loc for k in ["new zealand", "auckland", "wellington", "christchurch"]):
        return [
            f"https://www.seek.co.nz/{role_slug}-jobs",
            f"https://nz.indeed.com/jobs?q={role_q}&l={loc_q}",
        ]
    if any(k in loc for k in ["india", "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "chennai", "pune"]):
        return [
            f"https://www.naukri.com/{role_slug}-jobs",
            f"https://in.indeed.com/jobs?q={role_q}&l={loc_q}",
        ]
    if any(k in loc for k in ["philippines", "manila", "cebu", "davao"]):
        return [
            f"https://www.jobstreet.com.ph/en/job-search/{role_slug}-jobs/in-philippines/",
            f"https://www.kalibrr.com/job-board?role={role_q}",
        ]
    if any(k in loc for k in ["united kingdom", " uk ", "uk,", "london", "manchester", "birmingham", "leeds"]):
        return [
            f"https://www.reed.co.uk/jobs/{role_slug}-jobs",
            f"https://uk.indeed.com/jobs?q={role_q}&l={loc_q}",
        ]
    if any(k in loc for k in ["germany", "berlin", "munich", "hamburg", "frankfurt"]):
        return [
            f"https://www.stepstone.de/jobs/{role_slug}/in-deutschland.html",
            f"https://de.indeed.com/jobs?q={role_q}&l={loc_q}",
        ]
    # Global / US default
    return [
        f"https://www.indeed.com/jobs?q={role_q}&l={loc_q}",
        f"https://www.glassdoor.com/Job/{role_slug}-jobs.htm",
    ]


async def _extract_jobs_from_search_page(
    board_url: str, text: str, role: str, location: str, source: str
) -> list[dict]:
    """Claude Haiku extracts multiple job listings from a job board search results page."""
    currency = {
        "malaysia": "MYR", "kuala lumpur": "MYR", "kl": "MYR",
        "singapore": "SGD", "australia": "AUD", "new zealand": "NZD",
        "india": "INR", "philippines": "PHP", "united kingdom": "GBP",
        "london": "GBP", "germany": "EUR", "uk": "GBP",
    }.get(next((k for k in ["malaysia","kuala lumpur","singapore","australia",
                             "new zealand","india","philippines","united kingdom",
                             "london","germany","uk"] if k in location.lower()), ""), "USD")

    prompt = (
        f'Extract all visible job listings from this {source} search results page.\n'
        f'Role searched: "{role}". Location: "{location}".\n\n'
        "For each job listing found, return a JSON object with:\n"
        "title, company_name, location (string), "
        "salary_min (annual integer or null), salary_max (annual integer or null), "
        f"salary_currency (use {currency!r} for this location unless stated otherwise), "
        "seniority_level (entry/mid/senior/unknown), employment_type (full-time/part-time/contract), "
        "is_remote (boolean), job_description (any visible description text, min 30 chars).\n\n"
        "Return a JSON array. Return [] if no listings visible. Do not invent jobs.\n\n"
        f"Page text:\n{text}"
    )
    try:
        async with asyncio.timeout(15):
            resp = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=3000,
                system="Extract job listings from job board pages. Return valid JSON array only.",
                messages=[{"role": "user", "content": prompt}],
            )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        items = json.loads(raw)
        if not isinstance(items, list):
            return []

        results = []
        for j in items[:6]:
            if not j.get("title") or not j.get("company_name"):
                continue
            desc = j.get("job_description") or ""
            if len(desc) < 30:
                desc = f"{j.get('title')} at {j.get('company_name')} in {j.get('location') or location}."
            results.append({
                "job_description": desc,
                "title": str(j.get("title") or ""),
                "company_name": str(j.get("company_name") or ""),
                "url": board_url,
                "apply_link": board_url,
                "location": str(j.get("location") or location),
                "skills_listed": [],
                "date_posted": "",
                "num_applicants": None,
                "salary_min": float(j.get("salary_min") or 0),
                "salary_max": float(j.get("salary_max") or 0),
                "salary_currency": str(j.get("salary_currency") or currency),
                "is_remote": bool(j.get("is_remote", False)),
                "remote_type": "remote" if j.get("is_remote") else "on-site",
                "seniority_level": str(j.get("seniority_level") or "unknown").lower(),
                "employment_type": str(j.get("employment_type") or "full-time").lower(),
                "company_industry": "",
                "company_size": "",
                "company_description": "",
                "source": source,
                "is_verified": True,
                "extracted_skills": [],
                "freshness_score": 0.6,
                "competition_score": 0.5,
                "dual_signal": {},
                "cross_source_confirmed": {},
            })
        logger.info("Search page extraction: %d jobs from %s", len(results), source)
        return results
    except Exception as exc:
        logger.warning("Search page extraction failed for %s: %s", board_url, exc)
        return []


async def _search_direct_job_boards(role: str, location: str) -> list[dict]:
    """
    Skip SERP entirely — fetch job board search pages directly based on location,
    then extract multiple job listings from each page via Claude Haiku.
    Requires only Web Unlocker (no SERP dataset).
    """
    board_urls = _direct_board_urls(role, location)

    async def _fetch_board(url: str) -> list[dict]:
        source = _detect_job_source(url)
        try:
            html = await _fetch_with_unlocker(url)
            if not html or len(html) < 500:
                return []
            text = _html_to_text(html, max_chars=6000)
            return await _extract_jobs_from_search_page(url, text, role, location, source)
        except Exception as exc:
            logger.warning("Board fetch failed for %s: %s", url, exc)
            return []

    results = await asyncio.gather(*[_fetch_board(u) for u in board_urls[:3]])
    jobs: list[dict] = []
    for batch in results:
        jobs.extend(batch)
    logger.info("Direct boards: %d total jobs for '%s' in '%s'", len(jobs), role, location)
    return jobs[:8]


# ---------------------------------------------------------------------------
# Per-job gap analysis (F3)
# ---------------------------------------------------------------------------

@app.post("/api/analyse", dependencies=[Depends(_require_demo_secret)])
async def analyse_job(req: AnalyseRequest) -> dict:
    """
    Detailed gap analysis for a single job: Claude Sonnet synthesis.
    Addendum Q: Bright Data Web Unlocker fetches real job page content first.
    Falls back to scraped metadata if Unlocker fails or returns < 200 chars.
    """
    if not validate_job_url(req.job_url):
        return {"status": "error", "message": "Invalid job URL"}

    # Addendum Q: fetch real job page via Web Unlocker
    raw_html = await _fetch_with_unlocker(req.job_url)
    page_text = _html_to_text(raw_html) if raw_html else ""

    # Build metadata context from already-scraped fields
    job_context_lines = []
    if req.job_title:
        job_context_lines.append(f"Role: {req.job_title}")
    if req.company:
        job_context_lines.append(f"Company: {req.company}")
    if req.location:
        job_context_lines.append(f"Location: {req.location}")
    if req.seniority:
        job_context_lines.append(f"Seniority: {req.seniority}")
    if req.salary:
        job_context_lines.append(f"Salary range: {req.salary}")
    if req.skills_match:
        job_context_lines.append(f"Required skills already matched: {', '.join(req.skills_match)}")

    metadata_context = "\n".join(job_context_lines) or f"Job URL: {req.job_url}"

    if page_text:
        prompt = (
            f"A candidate wants to apply to this job posting.\n\n"
            f"**Job Metadata:**\n{metadata_context}\n\n"
            f"**Live Job Description (fetched via Bright Data Web Unlocker):**\n{page_text}\n\n"
            "Based on the full job description and metadata above, provide:\n"
            "1. Top 5 specific skills to highlight in the application\n"
            "2. Top 3 skill gaps likely expected for this role and seniority\n"
            "3. One specific, actionable tip tailored to this company and role to stand out\n\n"
            "Return JSON only:\n"
            '{{"highlight_skills": [...], "gap_skills": [...], "application_tip": "..."}}'
        )
    else:
        prompt = (
            f"A candidate wants to apply to this job posting:\n\n{metadata_context}\n\n"
            "Based on the role, company, seniority level, and matched skills above, provide:\n"
            "1. Top 5 specific skills to highlight in the application (use the matched skills as anchors)\n"
            "2. Top 3 skill gaps likely expected for this role and seniority that the candidate should address\n"
            "3. One specific, actionable tip tailored to this company and role to stand out\n\n"
            "Return JSON only:\n"
            '{{"highlight_skills": [...], "gap_skills": [...], "application_tip": "..."}}'
        )

    try:
        async with asyncio.timeout(20):
            response = await _client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                system="You are a career coach. Return valid JSON only.",
                messages=[{"role": "user", "content": prompt}],
            )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return {"status": "ok", "analysis": json.loads(text), "source": "unlocker" if page_text else "metadata"}
    except Exception as exc:
        logger.warning("Analyse failed: %s", exc)
        return {"status": "error", "message": "Analysis unavailable"}


# ---------------------------------------------------------------------------
# HR Competitor Intelligence (F9) — Track 1
# ---------------------------------------------------------------------------

@app.post("/api/hr/competitors", dependencies=[Depends(_require_demo_secret)])
async def hr_competitors(req: HRRequest) -> dict:
    """
    Scrape competitor hiring signals for HR teams.
    Returns skill demand breakdown for the target company + role.
    """
    if not req.company_name.strip() or not req.role.strip():
        return {"status": "error", "message": "Company name and role are required"}

    postings, _ = await _scrape_with_fallback(
        f"{req.role} {req.company_name}", req.location
    )
    if not postings:
        return {"status": "error", "message": "No competitor postings found"}

    enrich_postings(postings)
    jd_list = [p["job_description"] for p in postings if p["job_description"]]
    all_skills_lists = await extract_all(_client, jd_list)
    for p, skills in zip(postings, all_skills_lists):
        p["extracted_skills"] = skills
    compute_dual_signal(postings)
    compute_cross_source(postings)

    from scoring import skill_demand_score
    all_skills_flat = {s.lower() for sub in all_skills_lists for s in sub}
    skill_scores = {
        skill: skill_demand_score(skill, postings)
        for skill in all_skills_flat
    }
    top_skills = sorted(skill_scores.items(), key=lambda x: x[1], reverse=True)[:15]

    return {
        "status": "ok",
        "company": req.company_name,
        "role": req.role,
        "postings_analysed": len(postings),
        "top_skills": [{"skill": s, "score": sc} for s, sc in top_skills],
    }


# ---------------------------------------------------------------------------
# Company Profile — Addendum U
# SERP → Glassdoor URL → Web Unlocker → Claude Haiku extraction
# ---------------------------------------------------------------------------

def _company_serp_sync(company_name: str) -> dict:
    """
    Multi-source company SERP: discovers Glassdoor + Indeed URLs and extracts
    rating/review snippets from Google knowledge graph and SERP results.
    Returns: {glassdoor_url, indeed_url, serp_snippets, serp_rating}
    """
    import requests as _req
    import re

    token = os.environ.get("BRIGHTDATA_API_TOKEN", "")
    dataset = os.environ.get("BRIGHTDATA_DATASET_ID", "")
    if not token or not dataset:
        return {"glassdoor_url": "", "indeed_url": "", "serp_snippets": "", "serp_rating": None}

    keyword = f'"{company_name}" company reviews rating employees glassdoor indeed'
    payload = {
        "input": [{
            "url": "https://www.google.com/",
            "keyword": keyword,
            "language": "en",
            "tbs": "",
            "tbm": "",
            "uule": "",
            "nfpr": "",
            "index": "",
        }]
    }
    glassdoor_url = ""
    indeed_url = ""
    serp_rating = None
    snippets: list[str] = []

    try:
        resp = _req.post(
            f"https://api.brightdata.com/datasets/v3/scrape"
            f"?dataset_id={dataset}&notify=false&include_errors=true",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            data = {}

        # Knowledge graph may have aggregate rating
        kg = data.get("knowledge_graph") or {}
        for key in ("rating", "google_rating", "aggregate_rating"):
            val = kg.get(key)
            if val:
                try:
                    serp_rating = float(str(val).split("/")[0].split("out")[0].strip())
                    break
                except (ValueError, TypeError):
                    pass

        # Organic: collect URLs + snippets
        for r in (data.get("organic") or []):
            link = r.get("link", "")
            snippet = r.get("snippet", "")
            if snippet:
                snippets.append(snippet[:250])
            if not glassdoor_url and "glassdoor.com" in link and any(
                k in link for k in ("Reviews", "reviews", "Overview", "overview")
            ):
                glassdoor_url = link
            if not indeed_url and "indeed.com/cmp/" in link:
                indeed_url = link

        # Try rating from snippets (e.g. "4.1 out of 5 stars" or "Rating: 3.9")
        if serp_rating is None:
            combined = " ".join(snippets[:4])
            m = re.search(r"(\d\.\d)\s*(out of|/)\s*5", combined, re.I)
            if not m:
                m = re.search(r"[Rr]ating[:\s]+(\d\.\d)", combined)
            if m:
                try:
                    serp_rating = float(m.group(1))
                except (ValueError, IndexError):
                    pass

    except Exception as exc:
        logger.warning("Company SERP failed for %s: %s", company_name, exc)

    logger.info(
        "Company SERP '%s': glassdoor=%s indeed=%s rating=%s",
        company_name, bool(glassdoor_url), bool(indeed_url), serp_rating,
    )
    return {
        "glassdoor_url": glassdoor_url,
        "indeed_url": indeed_url,
        "serp_snippets": "\n".join(snippets[:5]),
        "serp_rating": serp_rating,
    }


@app.post("/api/company", dependencies=[Depends(_require_demo_secret)])
async def get_company_profile(req: CompanyRequest) -> dict:
    """
    Addendum U (enhanced): Multi-source company profile.
    Pipeline:
      Step 1: SERP → discover Glassdoor URL + Indeed URL + extract SERP snippets/rating
      Step 2: Web Unlocker (parallel) → fetch both pages
      Step 3: Claude Haiku → synthesize multi-source profile with per-source ratings
    Bright Data tools: SERP API + Web Unlocker (×2)
    """
    company = req.company_name.strip()
    if not company:
        return {"status": "error", "message": "Company name required"}

    # Step 1: Multi-source SERP
    try:
        async with asyncio.timeout(25):
            serp = await asyncio.to_thread(_company_serp_sync, company)
    except Exception:
        serp = {"glassdoor_url": "", "indeed_url": "", "serp_snippets": "", "serp_rating": None}

    glassdoor_url = serp["glassdoor_url"]
    indeed_url = serp["indeed_url"]
    serp_snippets = serp["serp_snippets"]
    serp_rating = serp["serp_rating"]

    # Step 2: Parallel Web Unlocker fetches (Glassdoor + Indeed)
    async def _safe_fetch(url: str, label: str) -> str:
        if not url:
            return ""
        try:
            html = await _fetch_with_unlocker(url)
            text = _html_to_text(html, max_chars=4000) if html else ""
            if text:
                logger.info("Company %s: %d chars from %s", label, len(text), url)
            return text
        except Exception as exc:
            logger.warning("Company fetch %s failed: %s", label, exc)
            return ""

    glassdoor_text, indeed_text = await asyncio.gather(
        _safe_fetch(glassdoor_url, "glassdoor"),
        _safe_fetch(indeed_url, "indeed"),
    )

    # Step 3: Build Claude context from all sources
    source_blocks = []
    sources_used = []
    if glassdoor_text:
        source_blocks.append(f"=== GLASSDOOR PAGE ===\n{glassdoor_text}")
        sources_used.append("glassdoor")
    if indeed_text:
        source_blocks.append(f"=== INDEED COMPANY PAGE ===\n{indeed_text}")
        sources_used.append("indeed")
    if serp_snippets:
        source_blocks.append(f"=== GOOGLE SEARCH SNIPPETS ===\n{serp_snippets}")
        sources_used.append("google")

    if source_blocks:
        combined_text = "\n\n".join(source_blocks)
        prompt = (
            f"Extract company review data for '{company}' from these web sources.\n\n"
            f"{combined_text}\n\n"
            "Return JSON only — use null for fields not found:\n"
            '{"glassdoor_rating": <float 1.0-5.0 or null>, '
            '"indeed_rating": <float 1.0-5.0 or null>, '
            '"overall_rating": <float 1.0-5.0 or null — best available>, '
            '"review_count": <int or null — total across sources>, '
            '"ceo_approval_pct": <int 0-100 or null>, '
            '"recommend_pct": <int 0-100 or null>, '
            '"pros": ["<employee quote or theme>", "<point>", "<point>", "<point>"], '
            '"cons": ["<employee quote or theme>", "<point>", "<point>", "<point>"], '
            '"employee_reviews": ['
            '{"text": "<verbatim or paraphrased review>", "rating": <1-5 or null>, "source": "<glassdoor|indeed|google>"},'
            '{"text": "...", "rating": null, "source": "..."}],'
            '"culture_summary": "<1-2 sentences>", '
            '"headquarters": "<city, country or null>", '
            '"founded": <year int or null>, '
            '"size_range": "<e.g. 1001-5000 employees or null>"}'
        )
        try:
            async with asyncio.timeout(15):
                resp = await _client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=600,
                    system="Extract structured company review data from web sources. Return valid JSON only.",
                    messages=[{"role": "user", "content": prompt}],
                )
            text = resp.content[0].text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            profile = json.loads(text)
            # Patch: if SERP found a rating but HTML didn't, use it
            if serp_rating and not profile.get("overall_rating"):
                profile["overall_rating"] = serp_rating
            return {
                "status": "ok",
                "company": company,
                "glassdoor_url": glassdoor_url or None,
                "indeed_url": indeed_url or None,
                "profile": profile,
                "sources_used": sources_used,
            }
        except Exception as exc:
            logger.warning("Company profile extraction failed for %s: %s", company, exc)

    # Fallback: Claude's own knowledge of the company
    try:
        async with asyncio.timeout(15):
            fallback_resp = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                system="You are a company intelligence analyst. Return valid JSON only.",
                messages=[{"role": "user", "content": (
                    f"Provide what you know about '{company}' as an employer. "
                    "Return JSON: "
                    '{"overall_rating": <float or null>, "review_count": null, '
                    '"ceo_approval_pct": null, "recommend_pct": null, '
                    '"pros": ["<known strength>", ...], "cons": ["<known challenge>", ...], '
                    '"employee_reviews": [], '
                    '"culture_summary": "<1-2 sentences based on general knowledge>", '
                    '"headquarters": "<city, country>", "founded": <year or null>, "size_range": "<range or null>"}'
                )}],
            )
        ft = fallback_resp.content[0].text.strip()
        if ft.startswith("```"):
            ft = ft.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        profile = json.loads(ft)
        return {
            "status": "ok",
            "company": company,
            "glassdoor_url": glassdoor_url or None,
            "indeed_url": indeed_url or None,
            "profile": profile,
            "sources_used": ["claude_knowledge"],
        }
    except Exception as exc:
        logger.warning("Company fallback Claude failed for %s: %s", company, exc)

    return {
        "status": "ok",
        "company": company,
        "glassdoor_url": None,
        "indeed_url": None,
        "profile": None,
        "sources_used": [],
    }


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tailorman — Addendum Z
# Resume-aware application tailoring: skills + job → tailored materials
# ---------------------------------------------------------------------------

@app.post("/api/tailorman", dependencies=[Depends(_require_demo_secret)])
async def tailorman(req: TailormanRequest) -> dict:
    """
    Addendum Z: Tailorman — personalised job application materials.
    Input: user's extracted skills + selected job details + gap analysis results.
    Output: tailored CV summary, skills to emphasise, cover letter opening,
            gap framing advice, interview talking points.
    No resume storage — uses extracted skill list only.
    """
    if not req.user_skills.strip() or not req.job_title.strip():
        return {"status": "error", "message": "Skills and job title are required"}

    target = f"{req.job_title}" + (f" at {req.job_company}" if req.job_company else "")
    highlight_str = ", ".join(req.highlight_skills) if req.highlight_skills else "not available"
    gap_str = ", ".join(req.gap_skills) if req.gap_skills else "none identified"
    seniority_str = f" ({req.seniority} level)" if req.seniority else ""

    prompt = (
        f"A job seeker{seniority_str} is applying for: {target}\n"
        f"Location: {req.job_location or 'not specified'}\n\n"
        f"Their skills: {req.user_skills}\n"
        f"Skills they already match for this role: {highlight_str}\n"
        f"Skill gaps for this role: {gap_str}\n\n"
        "Generate tailored application materials. Return JSON only:\n"
        '{\n'
        '  "tailored_summary": "<3-4 sentence CV/resume professional summary tailored to this exact role>",\n'
        '  "skills_to_emphasise": ["<skill1>", "<skill2>", "<skill3>", "<skill4>", "<skill5>"],\n'
        '  "cover_letter_opening": "<strong 2-3 sentence opening paragraph for a cover letter — reference the company and role>",\n'
        '  "gap_framing": "<1-2 sentences on how to address the skill gaps honestly and positively in the application>",\n'
        '  "interview_talking_points": [\n'
        '    {"point": "<specific talking point>", "why": "<why it resonates for this role>"},\n'
        '    {"point": "...", "why": "..."},\n'
        '    {"point": "...", "why": "..."}\n'
        '  ]\n'
        '}\n\n'
        "Be specific to this role and company. Do not give generic advice. Return ONLY the JSON."
    )

    try:
        async with asyncio.timeout(20):
            resp = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=700,
                system="You are an expert career coach and resume writer. Return valid JSON only.",
                messages=[{"role": "user", "content": prompt}],
            )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        return {"status": "ok", "tailored": result, "job": target}
    except Exception as exc:
        logger.warning("Tailorman failed for '%s': %s", req.job_title, exc)
        return {"status": "error", "message": "Tailoring unavailable — try again"}


# ---------------------------------------------------------------------------
# Salary intelligence — Addendum W
# ---------------------------------------------------------------------------

@app.post("/api/salary", dependencies=[Depends(_require_demo_secret)])
async def get_salary_intel(req: SalaryRequest) -> dict:
    """
    Addendum W: Market salary range + actionable increase tips.
    Uses Claude Haiku market knowledge for the role + location.
    Returns: market_min, market_median, market_max, currency, currency_symbol,
             market_context, increase_tips[{action, impact, timeframe}].
    """
    role = req.role.strip()
    if not role:
        return {"status": "error", "message": "Role is required"}

    location_ctx = req.location.strip() or "global market"
    prompt = (
        f"Role: {role}\n"
        f"Market: {location_ctx}\n\n"
        "Return JSON only with salary intelligence for this role in this market:\n"
        "{\n"
        '  "market_min": <annual min salary as integer>,\n'
        '  "market_median": <annual median salary as integer>,\n'
        '  "market_max": <annual max salary as integer>,\n'
        '  "currency": "<ISO code e.g. USD/MYR/SGD/GBP/AUD>",\n'
        '  "currency_symbol": "<symbol e.g. $/RM/S$/£/A$>",\n'
        '  "market_context": "<1-2 sentences on salary drivers and market conditions for this role in this location>",\n'
        '  "increase_tips": [\n'
        '    {"action": "<specific skill, cert, or career move>", "impact": "<salary increase range or %>", "timeframe": "<realistic time to achieve>"},\n'
        '    ... exactly 5 tips\n'
        '  ]\n'
        "}\n\n"
        "Use 2025/2026 market data. Use local currency — not USD if outside the US. "
        "For increase_tips: be specific — name exact tools, certifications, or skills, not generic advice. "
        "Return ONLY the JSON object, no explanation."
    )
    try:
        async with asyncio.timeout(20):
            resp = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                system="You are a compensation intelligence analyst. Return valid JSON only.",
                messages=[{"role": "user", "content": prompt}],
            )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        intel = json.loads(text)
        return {"status": "ok", "role": role, "location": req.location, "intel": intel}
    except Exception as exc:
        logger.warning("Salary intel failed for '%s' in '%s': %s", role, location_ctx, exc)
        return {"status": "error", "message": "Salary data unavailable"}


# ---------------------------------------------------------------------------
# Health check — Addendum J §26.1
# ---------------------------------------------------------------------------

@app.get("/health", include_in_schema=False)
async def health(request: Request) -> dict:
    uptime_s = time.monotonic() - _PROCESS_START
    h, rem = divmod(int(uptime_s), 3600)
    m, s = divmod(rem, 60)
    state = request.app.state
    return {
        "status": "ok",
        "uptime_s": round(uptime_s, 1),
        "uptime_human": f"{h}h {m}m {s}s",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "shadow_forced": getattr(state, "shadow_forced", False),
        "circuit_open": getattr(state, "circuit_open", False),
        "circuit_breaker_limit": CIRCUIT_BREAKER_LIMIT,
        "fallback_ready": getattr(state, "fallback_ready", False),
    }


# ---------------------------------------------------------------------------
# Admin status — Addendum G §23.4 (optional, Day 29)
# ---------------------------------------------------------------------------

@app.get("/api/debug/scrape", dependencies=[Depends(_require_demo_secret)])
async def debug_scrape(role: str = "Data Analyst", location: str = "Malaysia") -> dict:
    """Diagnose job discovery pipeline — tests each tier and reports what each returns."""
    out: dict = {"role": role, "location": location, "tiers": {}}

    # Env vars (presence only — no values)
    out["env"] = {
        "BRIGHTDATA_API_TOKEN": bool(os.environ.get("BRIGHTDATA_API_TOKEN")),
        "BRIGHTDATA_DATASET_ID": bool(os.environ.get("BRIGHTDATA_DATASET_ID")),
        "BRIGHTDATA_DATASET_ID_LINKEDIN": bool(os.environ.get("BRIGHTDATA_DATASET_ID_LINKEDIN")),
        "BRIGHTDATA_DATASET_ID_INDEED": bool(os.environ.get("BRIGHTDATA_DATASET_ID_INDEED")),
        "BRIGHTDATA_UNLOCKER_ZONE": os.environ.get("BRIGHTDATA_UNLOCKER_ZONE", "(not set)"),
        "DEMO_MODE": _DEMO_MODE,
    }

    # Tier 1: DDG plain HTTP
    try:
        job_urls, ddg_html = await _ddg_job_search(role, location)
        out["tiers"]["ddg_plain"] = {
            "html_bytes": len(ddg_html),
            "job_urls_found": len(job_urls),
            "sample_urls": job_urls[:3],
            "html_snippet": ddg_html[:400] if ddg_html else "",
        }
    except Exception as exc:
        out["tiers"]["ddg_plain"] = {"error": str(exc)}

    # Tier 2: Web Unlocker test (Jobstreet Malaysia search page)
    test_url = "https://www.jobstreet.com.my/en/job-search/data-analyst-jobs/in-malaysia/"
    try:
        html = await _fetch_with_unlocker(test_url)
        out["tiers"]["web_unlocker"] = {
            "url": test_url,
            "html_bytes": len(html),
            "working": len(html) > 500,
            "html_snippet": html[:300] if html else "",
        }
    except Exception as exc:
        out["tiers"]["web_unlocker"] = {"error": str(exc)}

    # Tier 3: Direct board URLs for location
    out["tiers"]["direct_board_urls"] = _direct_board_urls(role, location)

    # Tier 4: Plain GET to a board URL
    board_urls = _direct_board_urls(role, location)
    if board_urls:
        plain_html = await asyncio.to_thread(_plain_get_sync, board_urls[0])
        out["tiers"]["plain_get_board"] = {
            "url": board_urls[0],
            "html_bytes": len(plain_html),
            "working": len(plain_html) > 500,
            "html_snippet": plain_html[:300] if plain_html else "",
        }

    return out


@app.get("/api/admin/status")
async def admin_status(request: Request, admin_key: str = "") -> dict:
    """PRD §23.4. Returns firewall state. 404 if ADMIN_KEY not configured."""
    configured_key = os.environ.get("ADMIN_KEY", "")
    if not configured_key:
        raise HTTPException(status_code=404)
    if not _secrets.compare_digest(admin_key, configured_key):
        raise HTTPException(status_code=403)
    return {
        "live_search_count": request.app.state.live_search_count,
        "shadow_forced": request.app.state.shadow_forced,
        "circuit_breaker_limit": CIRCUIT_BREAKER_LIMIT,
        "reset_date": str(request.app.state.reset_date),
        "demo_secret_active": bool(_DEMO_SECRET),
    }
