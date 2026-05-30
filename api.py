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

_SCRAPE_TIMEOUT_S = int(os.environ.get("SCRAPE_TIMEOUT_S", "150"))
# Global scrape semaphore — cap at 1 concurrent scrape to stay within 512MB RAM on Render free tier.
# Two simultaneous scrapes (LinkedIn NDJSON + Claude extractions) exceed the limit.
_scrape_sem = asyncio.Semaphore(1)
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
    "malaysia": "my", "kuala lumpur": "my", "kl": "my", "penang": "my",
    "johor": "my", "selangor": "my", "petaling": "my", "subang": "my",
    "singapore": "sg", " sg": "sg",
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


def _currency_for_location(location: str) -> str:
    loc = location.lower()
    for kw, cur in [
        ("malaysia", "MYR"), ("kuala lumpur", "MYR"), ("penang", "MYR"), ("johor", "MYR"),
        ("singapore", "SGD"),
        ("australia", "AUD"), ("sydney", "AUD"), ("melbourne", "AUD"),
        ("new zealand", "NZD"), ("auckland", "NZD"),
        ("india", "INR"), ("bangalore", "INR"), ("mumbai", "INR"),
        ("philippines", "PHP"), ("manila", "PHP"),
        ("united kingdom", "GBP"), ("london", "GBP"),
        ("germany", "EUR"), ("berlin", "EUR"),
        ("canada", "CAD"), ("toronto", "CAD"),
    ]:
        if kw in loc:
            return cur
    return "USD"


def _extract_jsonld_jobs(html: str, source_url: str) -> list[dict]:
    """Parse <script type="application/ld+json"> JobPosting objects — zero Claude calls."""
    jobs: list[dict] = []
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    ):
        try:
            data = json.loads(block.strip())
            items = data if isinstance(data, list) else [data]
            # flatten @graph
            flat: list = []
            for item in items:
                if isinstance(item, dict) and "@graph" in item:
                    flat.extend(item["@graph"])
                else:
                    flat.append(item)
            for item in flat:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") != "JobPosting":
                    continue
                title = item.get("title") or item.get("name") or ""
                company = ""
                ho = item.get("hiringOrganization")
                if isinstance(ho, dict):
                    company = ho.get("name", "")
                elif isinstance(ho, str):
                    company = ho
                loc_str = ""
                jl = item.get("jobLocation")
                if isinstance(jl, dict):
                    addr = jl.get("address", {})
                    if isinstance(addr, dict):
                        parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
                        loc_str = ", ".join(p for p in parts if p)
                    elif isinstance(addr, str):
                        loc_str = addr
                elif isinstance(jl, list) and jl:
                    first = jl[0]
                    if isinstance(first, dict):
                        addr = first.get("address", {})
                        if isinstance(addr, dict):
                            parts = [addr.get("addressLocality"), addr.get("addressCountry")]
                            loc_str = ", ".join(p for p in parts if p)
                desc = item.get("description") or ""
                if isinstance(desc, list):
                    desc = " ".join(str(d) for d in desc)
                desc = re.sub(r"<[^>]+>", " ", str(desc))
                desc = re.sub(r"\s+", " ", desc).strip()
                salary_min = 0.0
                salary_max = 0.0
                currency = _currency_for_location(loc_str or source_url)
                bs = item.get("baseSalary")
                if isinstance(bs, dict):
                    currency = bs.get("currency", currency)
                    val = bs.get("value", {})
                    if isinstance(val, dict):
                        salary_min = float(val.get("minValue") or 0)
                        salary_max = float(val.get("maxValue") or 0)
                    elif isinstance(val, (int, float)):
                        salary_min = salary_max = float(val)
                apply_url = item.get("url") or source_url
                if not title or not company:
                    continue
                if len(desc) < 30:
                    desc = f"{title} at {company} in {loc_str or 'Unknown'}."
                jobs.append({
                    "job_description": desc[:2000],
                    "title": title,
                    "company_name": company,
                    "url": apply_url,
                    "apply_link": apply_url,
                    "location": loc_str,
                    "skills_listed": [],
                    "date_posted": item.get("datePosted") or "",
                    "num_applicants": None,
                    "salary_min": salary_min,
                    "salary_max": salary_max,
                    "salary_currency": currency,
                    "is_remote": item.get("jobLocationType") == "TELECOMMUTE",
                    "remote_type": "remote" if item.get("jobLocationType") == "TELECOMMUTE" else "on-site",
                    "seniority_level": "unknown",
                    "employment_type": str(item.get("employmentType") or "full-time").lower().replace("_", "-"),
                    "company_industry": "",
                    "company_size": "",
                    "company_description": "",
                    "source": _detect_job_source(source_url),
                    "is_verified": True,
                    "extracted_skills": [],
                    "freshness_score": 0.7,
                    "competition_score": 0.5,
                    "dual_signal": {},
                    "cross_source_confirmed": {},
                })
        except Exception:
            continue
    logger.info("JSON-LD: %d JobPosting objects from %s", len(jobs), source_url)
    return jobs[:8]


def _find_job_objects_in_json(obj, _depth: int = 0) -> list[dict]:
    """Recursively find arrays of job-like objects in a parsed JSON tree."""
    if _depth > 9 or not isinstance(obj, (dict, list)):
        return []
    _JOB_TITLE = {"title", "jobTitle", "positionTitle", "jobName", "name"}
    _JOB_CO    = {"companyName", "company", "advertiserName", "employer", "hiringOrganization"}
    if isinstance(obj, list) and len(obj) >= 1:
        sample = obj[0]
        if isinstance(sample, dict):
            if any(k in sample for k in _JOB_TITLE) and any(k in sample for k in _JOB_CO):
                return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for v in obj.values():
            found = _find_job_objects_in_json(v, _depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                found = _find_job_objects_in_json(item, _depth + 1)
                if found:
                    return found
    return []


def _extract_nextdata_jobs_direct(html: str, source_url: str) -> list[dict]:
    """
    Parse __NEXT_DATA__ JSON in full — recursively find job arrays,
    extract without Claude. Works for Jobstreet, Reed, and any Next.js job board.
    """
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    )
    if not m:
        return []
    try:
        data = json.loads(m.group(1).strip())
    except Exception:
        return []
    job_items = _find_job_objects_in_json(data)
    if not job_items:
        return []
    currency = _currency_for_location(source_url)
    results: list[dict] = []
    for item in job_items[:8]:
        title = (
            item.get("title") or item.get("jobTitle") or
            item.get("positionTitle") or item.get("name") or ""
        )
        company = (
            item.get("companyName") or item.get("company") or
            item.get("advertiserName") or item.get("employer") or
            (item.get("hiringOrganization") or {}).get("name", "") if isinstance(item.get("hiringOrganization"), dict) else
            item.get("hiringOrganization") or ""
        )
        loc_str = (
            item.get("locationLabel") or item.get("location") or
            item.get("suburb") or item.get("area") or ""
        )
        desc = (
            item.get("teaser") or item.get("description") or
            item.get("shortDescription") or item.get("snippet") or ""
        )
        apply_url = (
            item.get("jobUrl") or item.get("url") or
            item.get("applyUrl") or item.get("detailUrl") or source_url
        )
        if apply_url and apply_url.startswith("/"):
            from urllib.parse import urlparse as _up
            p = _up(source_url)
            apply_url = f"{p.scheme}://{p.netloc}{apply_url}"
        if not title or not company:
            continue
        if len(str(desc)) < 20:
            desc = f"{title} at {company}."
        results.append({
            "job_description": str(desc)[:1000],
            "title": str(title),
            "company_name": str(company),
            "url": apply_url,
            "apply_link": apply_url,
            "location": str(loc_str),
            "skills_listed": [],
            "date_posted": item.get("listingDate") or item.get("datePosted") or "",
            "num_applicants": None,
            "salary_min": 0.0,
            "salary_max": 0.0,
            "salary_currency": currency,
            "is_remote": False,
            "remote_type": "on-site",
            "seniority_level": "unknown",
            "employment_type": "full-time",
            "company_industry": "",
            "company_size": "",
            "company_description": "",
            "source": _detect_job_source(source_url),
            "is_verified": True,
            "extracted_skills": [],
            "freshness_score": 0.7,
            "competition_score": 0.5,
            "dual_signal": {},
            "cross_source_confirmed": {},
        })
    logger.info("__NEXT_DATA__ direct: %d jobs from %s", len(results), source_url)
    return results


def _extract_nextdata_text(html: str) -> str:
    """Extract __NEXT_DATA__ JSON as text for Claude fallback (when direct parse finds nothing)."""
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    )
    if not m:
        return ""
    # Take a middle slice — first 8K is often metadata, real data starts later
    raw = m.group(1).strip()
    # Try to find the first job-like substring and take surrounding context
    for marker in ('"jobTitle"', '"title":', '"companyName"', '"advertiserName"'):
        idx = raw.find(marker)
        if idx > 100:
            start = max(0, idx - 200)
            return raw[start:start + 8000]
    return raw[:8000]


async def _fetch_indeed_rss(role: str, location: str) -> list[dict]:
    """Indeed RSS feed via Web Unlocker — structured XML, no JS rendering required."""
    import xml.etree.ElementTree as ET
    from urllib.parse import quote_plus

    loc = location.lower()
    if any(k in loc for k in ["malaysia", "kuala lumpur", "penang", "johor"]):
        base = "https://malaysia.indeed.com/rss"
    elif "singapore" in loc:
        base = "https://sg.indeed.com/rss"
    elif any(k in loc for k in ["australia", "sydney", "melbourne", "brisbane"]):
        base = "https://au.indeed.com/rss"
    elif any(k in loc for k in ["new zealand", "auckland"]):
        base = "https://nz.indeed.com/rss"
    elif any(k in loc for k in ["india", "bangalore", "mumbai", "delhi"]):
        base = "https://in.indeed.com/rss"
    elif any(k in loc for k in ["philippines", "manila"]):
        base = "https://ph.indeed.com/rss"
    elif any(k in loc for k in ["united kingdom", "london"]):
        base = "https://uk.indeed.com/rss"
    else:
        base = "https://www.indeed.com/rss"

    url = f"{base}?q={quote_plus(role)}&l={quote_plus(location)}&sort=date"
    try:
        xml_text = await _fetch_with_unlocker(url)
        if not xml_text or len(xml_text) < 100:
            return []
        root = ET.fromstring(xml_text)
        channel = root.find("channel")
        if channel is None:
            return []
        currency = _currency_for_location(location)
        jobs: list[dict] = []
        for item in channel.findall("item")[:8]:
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            description = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
            description = re.sub(r"\s+", " ", description).strip()
            apply_url = (item.findtext("link") or "").strip()
            date_posted = (item.findtext("pubDate") or "").strip()
            company = ""
            src = item.find("source")
            if src is not None and src.text:
                company = src.text.strip()
            if len(description) < 30:
                description = f"{title} position in {location}."
            jobs.append({
                "job_description": description[:1000],
                "title": title,
                "company_name": company or "Unknown",
                "url": apply_url,
                "apply_link": apply_url,
                "location": location,
                "skills_listed": [],
                "date_posted": date_posted,
                "num_applicants": None,
                "salary_min": 0.0,
                "salary_max": 0.0,
                "salary_currency": currency,
                "is_remote": False,
                "remote_type": "on-site",
                "seniority_level": "unknown",
                "employment_type": "full-time",
                "company_industry": "",
                "company_size": "",
                "company_description": "",
                "source": "indeed",
                "is_verified": True,
                "extracted_skills": [],
                "freshness_score": 0.7,
                "competition_score": 0.5,
                "dual_signal": {},
                "cross_source_confirmed": {},
            })
        logger.info("Indeed RSS: %d jobs for '%s' in '%s'", len(jobs), role, location)
        return jobs
    except Exception as exc:
        logger.warning("Indeed RSS failed for '%s' in '%s': %s", role, location, exc)
        return []


async def _find_jobs_via_web(role: str, location: str) -> list[dict]:
    """
    Multi-tier job discovery — no hardcoded fallback, all real postings.
    Tier 0: Indeed RSS via Web Unlocker — structured XML, no JS rendering
    Tier 1: Bright Data Dataset SERP → pages via Web Unlocker + JSON-LD/NEXT_DATA extraction
    Tier 2: DDG plain (blocked on Render) → DDG via Web Unlocker SERP
    Tier 3: Direct job board search pages → JSON-LD/NEXT_DATA + Claude extraction
    Tier 4: Extract listings from DDG snippet text when all page fetches fail
    """
    # Tier 0: Indeed RSS — structured XML via Web Unlocker, no JS rendering needed
    rss_jobs = await _fetch_indeed_rss(role, location)
    if rss_jobs:
        return rss_jobs

    # Tier 1: Dataset API SERP
    job_urls: list[str] = await asyncio.to_thread(_serp_broad_jobs_sync, role, location)
    ddg_html = ""

    # Tier 2: DDG plain (blocked on Render — expected to return empty)
    if not job_urls:
        logger.info("Dataset SERP: 0 — trying DDG plain")
        job_urls, ddg_html = await _ddg_job_search(role, location)

    # Tier 2b: DDG via Web Unlocker (since plain is blocked on Render)
    if not job_urls:
        logger.info("DDG plain: 0 — trying DDG via Web Unlocker")
        job_urls = await _serp_via_unlocker(role, location)

    # Tier 3: Direct job board search pages
    if not job_urls:
        logger.info("DDG Unlocker: 0 — trying direct board URLs")
        job_urls = _direct_board_urls(role, location)

    # Fetch pages — use Scraping Browser for JS-heavy sites so jobs are rendered
    async def _fetch_safe(url: str) -> tuple[str, str]:
        try:
            html = await _fetch_with_browser(url)
            return url, html or ""
        except Exception:
            return url, ""

    fetch_results = await asyncio.gather(*[_fetch_safe(u) for u in job_urls[:6]])
    page_htmls = [(url, html) for url, html in fetch_results if html and len(html) > 500]

    if page_htmls:
        # Path A: JSON-LD structured data — zero Claude calls, works for most job boards
        jsonld_jobs: list[dict] = []
        for url, html in page_htmls:
            jsonld_jobs.extend(_extract_jsonld_jobs(html, url))
        if jsonld_jobs:
            logger.info("JSON-LD extraction: %d jobs for '%s'", len(jsonld_jobs), role)
            return jsonld_jobs[:8]

        # Path B: __NEXT_DATA__ direct parse — no Claude, works for Jobstreet/Reed (Next.js)
        nextdata_jobs: list[dict] = []
        for url, html in page_htmls[:3]:
            nd_jobs = _extract_nextdata_jobs_direct(html, url)
            nextdata_jobs.extend(nd_jobs)
        if nextdata_jobs:
            logger.info("__NEXT_DATA__ direct: %d jobs for '%s'", len(nextdata_jobs), role)
            return nextdata_jobs[:8]

        # Path B2: __NEXT_DATA__ via Claude (fallback — send contextual slice)
        nextdata_claude_jobs: list[dict] = []
        for url, html in page_htmls[:3]:
            nextdata_text = _extract_nextdata_text(html)
            if nextdata_text:
                source = _detect_job_source(url)
                nd_jobs = await _extract_jobs_from_search_page(url, nextdata_text, role, location, source)
                nextdata_claude_jobs.extend(nd_jobs)
        if nextdata_claude_jobs:
            logger.info("__NEXT_DATA__ Claude: %d jobs for '%s'", len(nextdata_claude_jobs), role)
            return nextdata_claude_jobs[:8]

        # Path C: content-aware text extraction — skips 80K header, reads actual job section
        all_listing_jobs: list[dict] = []
        for url, html in page_htmls[:3]:
            source = _detect_job_source(url)
            content_text = _html_to_text_content(html, max_chars=15000)
            if content_text:
                listing_jobs = await _extract_jobs_from_search_page(url, content_text, role, location, source)
                all_listing_jobs.extend(listing_jobs)
        if all_listing_jobs:
            logger.info("Content extraction: %d jobs for '%s'", len(all_listing_jobs), role)
            return all_listing_jobs[:8]

        # Path D: fallback to beginning of page (original path — short pages / individual job pages)
        page_texts = [(url, _html_to_text(html, max_chars=6000)) for url, html in page_htmls]
        extracted = await asyncio.gather(*[
            _extract_job_from_page(url, text) for url, text in page_texts
        ])
        jobs = [j for j in extracted if j is not None]
        if jobs:
            logger.info("Page text extraction: %d jobs for '%s'", len(jobs), role)
            return jobs

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
    user_skills: str | None = None   # comma-separated — required for real gap analysis


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


class InterviewPrepRequest(BaseModel):
    job_title: str
    company: str = ""
    location: str = ""
    seniority: str = ""
    gap_skills: list[str] = []
    highlight_skills: list[str] = []
    user_skills: str = ""


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
        "inferred_role": parsed.get("inferred_role", ""),
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
    # Strip " - subtitle" suffixes that CV parsers append (e.g. "Business Analyst - Automation & Analytics")
    # to prevent over-specific SERP queries that return 0 LinkedIn results.
    role_input = req.role.strip()
    if " - " in role_input:
        role_input = role_input.split(" - ")[0].strip()
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
    async with _scrape_sem:
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

    # Title relevance filter: drop jobs where the title shares no keyword with the searched role.
    # Prevents insurance/sales/HR roles surfacing when "Data Analyst" was searched.
    _role_keywords = {w.lower() for w in primary_title.split() if len(w) > 3}
    if _role_keywords:
        _filtered = [
            j for j in ranked_jobs
            if any(kw in j.get("title", "").lower() for kw in _role_keywords)
        ]
        if _filtered:
            ranked_jobs = _filtered
            logger.info("Title filter: %d/%d jobs kept for '%s'", len(_filtered), len(ranked_jobs), primary_title)

    # Location mismatch filter: drop jobs whose location is clearly a different country.
    # Catches LinkedIn returning a company's HQ address instead of the actual job location
    # (e.g. Agoda Bangkok job showing "San Jose, CA" for a KL search).
    _loc_input = req.location.lower()
    _COUNTRY_MARKERS: dict[str, list[str]] = {
        "my": ["malaysia", "kuala lumpur", "petaling", "selangor", "penang", "johor", "subang"],
        "sg": ["singapore"],
        "au": ["australia", "sydney", "melbourne", "brisbane", "perth"],
        "in": ["india", "bangalore", "mumbai", "delhi", "chennai", "hyderabad"],
        "ph": ["philippines", "manila"],
        "gb": ["united kingdom", "london", "manchester", "england"],
        "de": ["germany", "berlin", "munich"],
        "ca": ["canada", "toronto", "vancouver"],
    }
    _FOREIGN_MARKERS: list[str] = [
        "united states", ", ca", ", ny", ", tx", ", wa", ", fl", ", il",
        "san jose", "new york", "los angeles", "chicago", "seattle", "austin",
        "united kingdom", "london", "germany", "berlin", "france", "paris",
        "india", "bangalore", "mumbai", "delhi", "jakarta", "bangkok",
        "manila", "ho chi minh", "beijing", "shanghai", "tokyo", "seoul",
    ]
    # Determine if we searched a specific non-US country
    _searched_gl = _gl_for_location(_loc_input)
    if _searched_gl and _searched_gl != "us":
        _expected_markers = _COUNTRY_MARKERS.get(_searched_gl, [])
        if _expected_markers:
            def _loc_ok(job: dict) -> bool:
                jloc = job.get("location", "").lower()
                if not jloc:
                    return True  # no location — keep (scraper may not have parsed it)
                if any(m in jloc for m in _expected_markers):
                    return True  # matches expected country
                if any(m in jloc for m in _FOREIGN_MARKERS):
                    return False  # clearly another country
                return True  # unknown location — keep
            _geo_filtered = [j for j in ranked_jobs if _loc_ok(j)]
            if _geo_filtered:
                removed = len(ranked_jobs) - len(_geo_filtered)
                if removed:
                    logger.info("Location filter: dropped %d jobs with mismatched country for '%s'", removed, req.location)
                ranked_jobs = _geo_filtered

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

# Bright Data Scraping Browser — full JS execution for SPA/React job boards
# Same REST endpoint as Web Unlocker but uses a browser zone.
# Set BRIGHTDATA_BROWSER_ZONE in Render to the name of your scraping_browser zone.
_BROWSER_ZONE = os.environ.get("BRIGHTDATA_BROWSER_ZONE", "")
_BROWSER_TIMEOUT_S = 60


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


async def _fetch_with_browser(url: str) -> str:
    """Bright Data Scraping Browser via Playwright CDP.

    Connects to Bright Data's remote browser over WebSocket — no local binary needed.
    Executes JavaScript, waits for job cards to render, returns fully rendered HTML.
    Falls back to Web Unlocker if credentials not configured or Playwright fails.
    """
    customer = os.environ.get("BRIGHTDATA_CUSTOMER_ID", "")
    password = os.environ.get("BRIGHTDATA_BROWSER_PASSWORD", "")
    if not customer or not password or not _BROWSER_ZONE:
        return await _fetch_with_unlocker(url)

    # Method 1: HTTP proxy — Bright Data browser renders JS and returns HTML via proxy
    # Simpler than CDP, doesn't need playwright installed
    proxy_url = (
        f"http://brd-customer-{customer}-zone-{_BROWSER_ZONE}"
        f":{password}@brd.superproxy.io:9222"
    )
    try:
        import requests as _req
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        text = await asyncio.to_thread(
            lambda: _req.get(
                url,
                proxies={"http": proxy_url, "https": proxy_url},
                verify=False,
                timeout=_BROWSER_TIMEOUT_S,
                headers={**_PLAIN_HEADERS, "Accept-Encoding": "identity"},
            ).text
        )
        if text and len(text) >= 1000:
            logger.info("Scraping Browser proxy: %d chars from %s", len(text), url)
            return text
        logger.warning("Scraping Browser proxy: too short (%d chars)", len(text or ""))
    except Exception as exc:
        logger.warning("Scraping Browser proxy failed for %s: %s", url, exc)

    # Method 2: Playwright CDP fallback
    cdp_url = (
        f"wss://brd-customer-{customer}-zone-{_BROWSER_ZONE}"
        f":{password}@brd.superproxy.io:9222"
    )
    try:
        from playwright.async_api import async_playwright  # type: ignore
        async with asyncio.timeout(_BROWSER_TIMEOUT_S + 10):
            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(cdp_url)
                page = await browser.new_page()
                await page.goto(url, wait_until="networkidle", timeout=55000)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.4)")
                for selector in (
                    '[data-automation="jobCard"]', '[data-automation="normalJob"]',
                    '[data-search-sol-meta]', '.job-card',
                ):
                    try:
                        await page.wait_for_selector(selector, timeout=8000)
                        break
                    except Exception:
                        continue
                html = await page.content()
                await browser.close()
        if html and len(html) >= 1000:
            logger.info("Scraping Browser CDP: %d chars from %s", len(html), url)
            return html
    except ImportError:
        logger.warning("playwright not installed")
    except Exception as exc:
        logger.warning("Scraping Browser CDP failed for %s: %s — falling back", url, exc)

    return await _fetch_with_unlocker(url)


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


def _html_to_text_content(html: str, max_chars: int = 15000) -> str:
    """
    Extract text from the CONTENT section of long JS-heavy pages.
    Navigation/header/scripts occupy the first ~80KB of React/Next.js pages.
    Skipping to byte 80K+ dramatically increases the chance of hitting job listings.
    Falls back to beginning if the page is short.
    """
    if len(html) > 120_000:
        # Start past the header/nav/CSS/script preamble
        start = 80_000
        # Take a wide window — job listings can span 100K+ bytes
        section = html[start:start + 300_000]
    else:
        section = html
    text = re.sub(r"<[^>]+>", " ", section)
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

    if any(k in loc for k in ["malaysia", "kuala lumpur", "kl", "penang", "johor", "selangor", "petaling", "subang"]):
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
        f'Extract job listings RELEVANT TO "{role}" from this {source} search results page.\n'
        f'Location context: "{location}".\n\n'
        f'IMPORTANT: Only include jobs where the title is directly related to "{role}" '
        f'(e.g. Data Analyst, Senior Data Analyst, Business Analyst, Analytics Engineer). '
        f'Skip unrelated jobs (e.g. Sales, Marketing, HR, Engineering if role is Data Analyst).\n\n'
        "For each matching job listing, return a JSON object with:\n"
        "title, company_name, location (string), "
        "salary_min (annual integer or null), salary_max (annual integer or null), "
        f"salary_currency (use {currency!r} for this location unless stated otherwise), "
        "seniority_level (entry/mid/senior/unknown), employment_type (full-time/part-time/contract), "
        "is_remote (boolean), job_description (any visible description text, min 30 chars).\n\n"
        f"Return a JSON array of only relevant jobs. Return [] if none match '{role}'. Do not invent jobs.\n\n"
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
            # Use Scraping Browser for JS-heavy boards (Jobstreet, Indeed)
            # so job listings are rendered before extraction
            html = await _fetch_with_browser(url)
            if not html or len(html) < 500:
                return []
            # Structured extraction — zero Claude calls, fastest path
            jsonld = _extract_jsonld_jobs(html, url)
            if jsonld:
                return jsonld[:6]
            nd_direct = _extract_nextdata_jobs_direct(html, url)
            if nd_direct:
                return nd_direct[:6]
            # Claude fallback: contextual __NEXT_DATA__ slice, then content section
            nextdata_text = _extract_nextdata_text(html)
            if nextdata_text:
                nd_jobs = await _extract_jobs_from_search_page(url, nextdata_text, role, location, source)
                if nd_jobs:
                    return nd_jobs
            content_text = _html_to_text_content(html, max_chars=15000)
            return await _extract_jobs_from_search_page(url, content_text, role, location, source)
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

    # Require user skills — gap analysis without knowing what the user has is meaningless
    user_skills_raw = (req.user_skills or "").strip()
    if not user_skills_raw:
        return {"status": "no_skills", "message": "Upload your CV or enter your skills to see YOUR personal skill gaps."}

    # Addendum Q: fetch real job page via Web Unlocker
    raw_html = await _fetch_with_unlocker(req.job_url)
    page_text = _html_to_text(raw_html) if raw_html else ""

    # Compute real gap = job skills user does NOT have
    # Compute real highlights = job skills user DOES have
    user_set = {s.strip().lower() for s in user_skills_raw.split(",") if s.strip()}
    job_skills = [s.strip() for s in (req.skills_match or []) if s.strip()]
    real_highlights = [s for s in job_skills if s.lower() in user_set]
    real_gaps = [s for s in job_skills if s.lower() not in user_set]

    # Build metadata context
    job_context_lines = [f"Role: {req.job_title or 'Unknown'}"]
    if req.company:      job_context_lines.append(f"Company: {req.company}")
    if req.location:     job_context_lines.append(f"Location: {req.location}")
    if req.seniority:    job_context_lines.append(f"Seniority: {req.seniority}")
    if req.salary:       job_context_lines.append(f"Salary range: {req.salary}")
    job_context_lines.append(f"Candidate's skills: {user_skills_raw}")
    job_context_lines.append(f"Job skills the candidate ALREADY HAS: {', '.join(real_highlights) or 'none matched'}")
    job_context_lines.append(f"Job skills the candidate is MISSING: {', '.join(real_gaps) or 'none identified from metadata'}")

    metadata_context = "\n".join(job_context_lines)

    if page_text:
        prompt = (
            f"A candidate wants to apply to this job. Here is their situation:\n\n"
            f"{metadata_context}\n\n"
            f"**Live Job Description (fetched via Bright Data Web Unlocker):**\n{page_text}\n\n"
            "Using the candidate's ACTUAL skills above, identify:\n"
            "1. highlight_skills: skills from the candidate's profile that match this job well (be specific, use skills they listed)\n"
            "2. gap_skills: skills this job requires that the candidate does NOT have yet (based on the job description)\n"
            "3. application_tip: one specific, actionable tip for THIS candidate at THIS company to stand out\n\n"
            "Return JSON only:\n"
            '{{"highlight_skills": [...], "gap_skills": [...], "application_tip": "..."}}'
        )
    else:
        prompt = (
            f"A candidate wants to apply to this job. Here is their situation:\n\n"
            f"{metadata_context}\n\n"
            "Using the candidate's ACTUAL skills above, identify:\n"
            "1. highlight_skills: skills from the candidate's profile that match this job well\n"
            "2. gap_skills: skills this job likely requires that the candidate does NOT have\n"
            "3. application_tip: one specific, actionable tip for THIS candidate at THIS company\n\n"
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

class HRDetectRequest(BaseModel):
    your_company: str
    role: str = ""
    location: str = ""


@app.post("/api/hr/detect-competitor", dependencies=[Depends(_require_demo_secret)])
async def hr_detect_competitor(req: HRDetectRequest) -> dict:
    """
    Step 1 of competitive intelligence:
    1. Claude identifies the top competitor for your company + role
    2. Bright Data SERP fetches news/market trend snippets
    """
    # Step A: Claude identifies top competitor
    competitor_prompt = (
        f"Company: {req.your_company}. Role they're analyzing: {req.role or 'general tech'}. "
        f"Location: {req.location or 'Southeast Asia'}.\n\n"
        "Return JSON only:\n"
        '{"competitor": "<single most relevant direct competitor company name — use exact LinkedIn company name>", '
        '"reason": "<one sentence why this is the main competitor>"}'
    )
    competitor = req.your_company  # fallback
    try:
        async with asyncio.timeout(10):
            resp = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                system="You are a market analyst. Return valid JSON only.",
                messages=[{"role": "user", "content": competitor_prompt}],
            )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        competitor = json.loads(text).get("competitor", req.your_company)
        logger.info("Detected competitor for '%s': '%s'", req.your_company, competitor)
    except Exception as exc:
        logger.warning("Competitor detection failed: %s", exc)

    # Step B: SERP for market news / trend snippets
    news_snippets = ""
    try:
        role_kw = req.role or "technology"
        loc_kw = req.location or "Southeast Asia"
        keyword = f"{req.your_company} {role_kw} {loc_kw} hiring trends 2025"
        payload = {"input": [{"url": "https://www.google.com/", "keyword": keyword,
                               "language": "en", "tbs": "qdr:y", "tbm": ""}]}
        import requests as _req_lib
        serp_resp = await asyncio.to_thread(
            lambda: _req_lib.post(
                f"https://api.brightdata.com/datasets/v3/scrape?dataset_id={os.environ.get('BRIGHTDATA_DATASET_ID','')}&include_errors=true",
                headers={"Authorization": f"Bearer {os.environ.get('BRIGHTDATA_API_TOKEN','')}", "Content-Type": "application/json"},
                json=payload, timeout=20,
            )
        )
        if serp_resp.ok:
            data = serp_resp.json()
            if isinstance(data, list): data = data[0] if data else {}
            organic = data.get("organic", []) if isinstance(data, dict) else []
            snippets = [r.get("description", "") for r in organic[:4] if r.get("description")]
            news_snippets = " | ".join(snippets)
            logger.info("News snippets fetched: %d chars", len(news_snippets))
    except Exception as exc:
        logger.warning("News SERP failed: %s", exc)

    return {"status": "ok", "competitor": competitor, "news_snippets": news_snippets}


@app.post("/api/hr/competitors", dependencies=[Depends(_require_demo_secret)])
async def hr_competitors(req: HRRequest) -> dict:
    """
    Scrape competitor hiring signals for HR teams.
    Returns skill demand breakdown for the target company + role.
    """
    if not req.company_name.strip() or not req.role.strip():
        return {"status": "error", "message": "Company name and role are required"}

    # Normalize role — corrects typos via Claude Haiku, then strips subtitle suffix.
    # Uses canonical_titles regardless of is_valid_role so partial words like
    # "Data Enginee" still resolve to "Data Engineer".
    role_input = req.role.strip()
    if " - " in role_input:
        role_input = role_input.split(" - ")[0].strip()
    try:
        async with asyncio.timeout(10):
            _norm_resp = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=60,
                system="You are a job title corrector. Return the most likely intended job title as plain text only. No explanation.",
                messages=[{"role": "user", "content": f"Correct this job title (fix typos/truncation): '{role_input}'"}],
            )
        normalized_role = _norm_resp.content[0].text.strip().strip('"').strip("'")
        if not normalized_role or len(normalized_role) > 80:
            normalized_role = role_input
        logger.info("HR role normalized: '%s' → '%s'", role_input, normalized_role)
    except Exception:
        normalized_role = role_input

    # Use LinkedIn-only scrape — skip web extraction fallback (too memory-heavy for HR path).
    # Semaphore ensures only 1 concurrent scrape runs — prevents OOM on 512MB Render free tier.
    try:
        from scraper import scrape_jobs
        async with _scrape_sem:
            postings = await asyncio.wait_for(
                asyncio.to_thread(scrape_jobs, f"{normalized_role} {req.company_name}", req.location),
                timeout=_SCRAPE_TIMEOUT_S,
            )
    except (asyncio.TimeoutError, Exception) as exc:
        logger.warning("HR scrape failed: %s", exc)
        postings = []
    if not postings:
        return {"status": "error", "message": "No competitor postings found. Check the company name and role spelling."}

    # Filter: keep only postings where company name matches the target (case-insensitive).
    # Prevents Agoda/EPAM postings bleeding into a Grab scan.
    company_lower = req.company_name.strip().lower()
    postings_filtered = [
        p for p in postings
        if company_lower in (p.get("company", "") or "").lower()
        or company_lower in (p.get("url", "") or "").lower()
    ]
    # Only apply filter if it leaves enough results; otherwise keep all (avoids empty scans)
    if len(postings_filtered) >= 3:
        postings = postings_filtered
        logger.info("HR company filter: kept %d/%d postings for '%s'", len(postings), len(postings_filtered), req.company_name)

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
    # Drop garbled/truncated skill tokens: must be ≥4 chars, at least 2 alpha chars,
    # not starting with a non-alpha character (e.g. "ead modeling" from "lead modeling")
    def _valid_skill(s: str) -> bool:
        if len(s) < 4:
            return False
        if sum(c.isalpha() for c in s) < 2:
            return False
        if not s[0].isalpha():
            return False
        return True

    top_skills = sorted(
        [(s, sc) for s, sc in skill_scores.items() if _valid_skill(s)],
        key=lambda x: x[1], reverse=True
    )[:15]

    posting_urls = [p.get("url", "") for p in postings if p.get("url")]

    return {
        "status": "ok",
        "company": req.company_name,
        "role": req.role,
        "postings_analysed": len(postings),
        "top_skills": [{"skill": s, "score": sc} for s, sc in top_skills],
        "posting_urls": posting_urls,
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


class HRRecommendationRequest(BaseModel):
    company: str
    role: str = ""
    top_skills: str  # comma-separated
    your_skills: str = ""  # comma-separated current team skills for gap comparison


@app.post("/api/hr/recommendations", dependencies=[Depends(_require_demo_secret)])
async def hr_recommendations(req: HRRecommendationRequest) -> dict:
    """Training roadmap: 3 priority skills to develop, with real course links."""
    # Pick top 3 skills to focus roadmap on
    skills_list = [s.strip() for s in req.top_skills.split(",") if s.strip()][:3]
    skills_focus = ", ".join(skills_list)

    prompt = (
        f"Skills needed for {req.role or 'this role'}: {skills_focus}.\n\n"
        "For each skill return a training roadmap entry. Use real, specific course URLs.\n"
        'Return JSON only — an array of exactly 3 objects:\n'
        '[{"skill":"python","why":"Core language for all data work","steps":["Complete Python basics on Codecademy","Build 2 data projects on GitHub","Practice LeetCode easy/medium"],"resource":"Codecademy Python Course","link":"https://www.codecademy.com/learn/learn-python-3","timeline":"4 weeks"},'
        '{"skill":"...","why":"...","steps":["...","...","..."],"resource":"...","link":"https://...","timeline":"..."},'
        '{"skill":"...","why":"...","steps":["...","...","..."],"resource":"...","link":"https://...","timeline":"..."}]'
    )
    try:
        async with asyncio.timeout(25):
            resp = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1200,
                system="You are an L&D specialist. Return valid JSON array only. Use real course URLs.",
                messages=[{"role": "user", "content": prompt}],
            )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        roadmap = json.loads(text)
        if not isinstance(roadmap, list):
            roadmap = roadmap.get("training_roadmap", [])
        return {"status": "ok", "training_roadmap": roadmap}
    except Exception as exc:
        logger.warning("HR recommendations failed: %s", exc)
        return {"status": "error", "message": "Could not generate recommendations"}


class HRIntelligenceRequest(BaseModel):
    company: str
    role: str = ""
    top_skills: str
    your_skills: str = ""
    news_context: str = ""  # SERP news snippets for richer analysis


@app.post("/api/hr/intelligence", dependencies=[Depends(_require_demo_secret)])
async def hr_intelligence(req: HRIntelligenceRequest) -> dict:
    """Competitive intelligence: interpret skill hiring signals strategically."""
    your_context = (
        f"Your team's current skills: {req.your_skills}.\n"
        if req.your_skills.strip() else
        "Your team's skills: not provided (give general industry gap advice).\n"
    )
    gap_instruction = (
        '"gap": "<2 sentences: compare YOUR TEAM\'s skills vs what the competitor needs — name the specific skills your team is missing from the competitor\'s list>",'
        if req.your_skills.strip() else
        '"gap": "<2 sentences: what skills companies NOT investing in these will lack vs this competitor in 12-18 months>",'
    )
    news_section = f"Market news/trends (from Bright Data SERP): {req.news_context}\n" if req.news_context else ""
    prompt = (
        f"Company/industry scanned: {req.company}. Role: {req.role or 'general'}.\n"
        f"Skills they are hiring for (live LinkedIn data via Bright Data): {req.top_skills}\n"
        f"{news_section}"
        f"{your_context}\n"
        "Analyse as a competitive intelligence analyst using both job posting signals and market news. Return JSON only:\n"
        '{"building": "<2 sentences: what capability the scanned company is building — reference both skill signals AND market trends if available>", '
        f"{gap_instruction} "
        '"action": "<2 sentences: one concrete action the HR team should take in the next 30 days — be specific to this company/role/market>"}'
    )
    try:
        async with asyncio.timeout(15):
            resp = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=350,
                system="You are a competitive intelligence analyst. Return valid JSON only.",
                messages=[{"role": "user", "content": prompt}],
            )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        return {"status": "ok", **data}
    except Exception as exc:
        logger.warning("HR intelligence failed: %s", exc)
        return {"status": "error"}


class HROutreachRequest(BaseModel):
    company: str
    role: str = ""
    top_skills: str
    candidate_profile: str  # name/URL/background the HR entered


class HRTalentHuntRequest(BaseModel):
    company: str
    role: str = ""
    top_skills: str
    location: str = ""


@app.post("/api/hr/talent-hunt", dependencies=[Depends(_require_demo_secret)])
async def hr_talent_hunt(req: HRTalentHuntRequest) -> dict:
    """
    Generate 3 candidate personas + search LinkedIn for real profile URLs via Bright Data SERP.
    """
    # Step 1: Generate personas via Claude
    prompt = (
        f"You are a headhunter. Hiring company: {req.company or 'client'}. Role: {req.role or 'talent'}. "
        f"Required skills: {req.top_skills}. Market: {req.location or 'Southeast Asia'}.\n\n"
        "Generate 3 distinct candidate personas. Each is a real type of person who has these skills.\n"
        "Return JSON array only — no wrapper object:\n"
        '[{"title":"<e.g. The MLOps Practitioner>","background":"<2 sentences: who they are, current role type, company type>",'
        '"likely_at":["<company 1>","<company 2>","<company 3>"],'
        '"search_query":"<Google search query to find their LinkedIn profiles: site:linkedin.com/in + 2 key skills + location>","outreach":"<4-5 sentence LinkedIn message: specific hook + 2 skills + soft CTA, first person>"},'
        '{"title":"...","background":"...","likely_at":["...","...","..."],"search_query":"...","outreach":"..."},'
        '{"title":"...","background":"...","likely_at":["...","...","..."],"search_query":"...","outreach":"..."}]'
    )
    personas = []
    try:
        async with asyncio.timeout(25):
            resp = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=900,
                system="You are an expert headhunter. Return valid JSON array only.",
                messages=[{"role": "user", "content": prompt}],
            )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(text)
        personas = parsed if isinstance(parsed, list) else parsed.get("personas", [])
    except Exception as exc:
        logger.warning("HR talent hunt persona generation failed: %s", exc)
        return {"status": "error", "message": "Could not generate talent hunt"}

    # Step 2: SERP search for real LinkedIn profile URLs (one call per persona, parallel)
    import requests as _req_lib

    async def _serp_profiles(search_query: str) -> list[str]:
        try:
            payload = {"input": [{"url": "https://www.google.com/", "keyword": search_query,
                                   "language": "en", "tbs": "", "tbm": ""}]}
            r = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: _req_lib.post(
                        f"https://api.brightdata.com/datasets/v3/scrape?dataset_id={os.environ.get('BRIGHTDATA_DATASET_ID','')}&include_errors=true",
                        headers={"Authorization": f"Bearer {os.environ.get('BRIGHTDATA_API_TOKEN','')}", "Content-Type": "application/json"},
                        json=payload, timeout=15,
                    )
                ), timeout=18
            )
            data = r.json()
            if isinstance(data, list): data = data[0] if data else {}
            organic = data.get("organic", []) if isinstance(data, dict) else []
            return [x["link"] for x in organic if "linkedin.com/in/" in x.get("link", "")][:3]
        except Exception:
            return []

    profile_results = await asyncio.gather(*[
        _serp_profiles(p.get("search_query", "")) for p in personas[:3]
    ])

    for persona, profile_urls in zip(personas, profile_results):
        persona["profile_urls"] = profile_urls

    return {"status": "ok", "personas": personas}


@app.post("/api/hr/outreach", dependencies=[Depends(_require_demo_secret)])
async def hr_outreach(req: HROutreachRequest) -> dict:
    """Generate a personalised LinkedIn outreach message for a specific candidate."""
    prompt = (
        f"You are an HR recruiter at a company hiring for: {req.role or 'this role'}.\n"
        f"The candidate you want to reach out to: {req.candidate_profile}\n"
        f"Key skills you need (from live market data): {req.top_skills}\n\n"
        "Write a personalised LinkedIn connection message (4-6 sentences) to invite this candidate to explore the role.\n"
        "Rules:\n"
        "- Open with a specific hook based on the candidate's background\n"
        "- Mention 2 skills from the list that match their profile\n"
        "- Keep it conversational, not corporate\n"
        "- End with a soft, no-pressure call to action\n"
        "- Write in first person as the recruiter\n"
        "- No subject line, just the message body\n\n"
        'Return JSON only: {"message": "<the outreach message>"}'
    )
    try:
        async with asyncio.timeout(20):
            resp = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                system="You are a professional recruiter. Return valid JSON only.",
                messages=[{"role": "user", "content": prompt}],
            )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        return {"status": "ok", "message": data.get("message", "")}
    except Exception as exc:
        logger.warning("HR outreach failed: %s", exc)
        return {"status": "error", "message": "Could not generate outreach message"}


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
# Interview Prep — Addendum AA
# Predicts likely interview questions + tips based on job + skill gaps
# ---------------------------------------------------------------------------

@app.post("/api/interview", dependencies=[Depends(_require_demo_secret)])
async def interview_prep(req: InterviewPrepRequest) -> dict:
    """
    Addendum AA: Predict interview questions and answer tips.
    Uses job title, company, seniority, gap skills, and user skills.
    Returns 5 questions with context-specific answer guidance.
    """
    if not req.job_title.strip():
        return {"status": "error", "message": "Job title required"}

    target = f"{req.job_title}" + (f" at {req.company}" if req.company else "")
    gap_str = ", ".join(req.gap_skills) if req.gap_skills else "none identified"
    highlight_str = ", ".join(req.highlight_skills) if req.highlight_skills else "not provided"
    seniority_str = f" ({req.seniority} level)" if req.seniority else ""

    prompt = (
        f"A candidate{seniority_str} is preparing for an interview for: {target}\n"
        f"Location: {req.location or 'not specified'}\n"
        f"Their strong skills: {highlight_str}\n"
        f"Their skill gaps for this role: {gap_str}\n"
        f"Their full skill set: {req.user_skills or 'not provided'}\n\n"
        "Generate 5 likely interview questions for this specific role and company, "
        "with a concise tip for answering each one. Focus on questions that probe "
        "the skill gaps and test the candidate's genuine expertise.\n\n"
        "Return JSON only:\n"
        '{"questions": ['
        '{"question": "<interview question>", "tip": "<specific 1-sentence answer tip>", "type": "technical|behavioural|situational"},'
        '... 5 items'
        ']}'
    )
    try:
        async with asyncio.timeout(20):
            resp = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                system="You are an expert interview coach. Return valid JSON only.",
                messages=[{"role": "user", "content": prompt}],
            )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        return {"status": "ok", "questions": result.get("questions", []), "job": target}
    except Exception as exc:
        logger.warning("Interview prep failed for '%s': %s", req.job_title, exc)
        return {"status": "error", "message": "Interview prep unavailable — try again"}


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

@app.api_route("/health", methods=["GET", "HEAD"], include_in_schema=False)
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

@app.get("/api/debug/connectivity")
async def debug_connectivity() -> dict:
    """No-auth outbound connectivity test — safe to expose, returns no user data."""
    ddg_html = await asyncio.to_thread(
        _plain_get_sync,
        "https://html.duckduckgo.com/html/?q=data+analyst+jobs+malaysia",
        15,
    )
    indeed_html = await asyncio.to_thread(
        _plain_get_sync,
        "https://malaysia.indeed.com/jobs?q=data+analyst",
        10,
    )
    jobstreet_html = await asyncio.to_thread(
        _plain_get_sync,
        "https://www.jobstreet.com.my/en/job-search/data-analyst-jobs/in-malaysia/",
        10,
    )
    token_present = bool(os.environ.get("BRIGHTDATA_API_TOKEN"))
    zone = os.environ.get("BRIGHTDATA_UNLOCKER_ZONE", "(not set)")

    unlocker_bytes = 0
    unlocker_error = ""
    if token_present:
        try:
            html = await _fetch_with_unlocker(
                "https://www.jobstreet.com.my/en/job-search/data-analyst-jobs/in-malaysia/"
            )
            unlocker_bytes = len(html)
        except Exception as exc:
            unlocker_error = str(exc)

    return {
        "ddg": {"bytes": len(ddg_html), "accessible": len(ddg_html) > 300, "sample": ddg_html[:300]},
        "indeed_plain": {"bytes": len(indeed_html), "accessible": len(indeed_html) > 300},
        "jobstreet_plain": {"bytes": len(jobstreet_html), "accessible": len(jobstreet_html) > 300},
        "web_unlocker": {
            "token_present": token_present,
            "zone": zone,
            "bytes": unlocker_bytes,
            "working": unlocker_bytes > 500,
            "error": unlocker_error,
        },
        "env": {
            "BRIGHTDATA_DATASET_ID": bool(os.environ.get("BRIGHTDATA_DATASET_ID")),
            "BRIGHTDATA_DATASET_ID_LINKEDIN": bool(os.environ.get("BRIGHTDATA_DATASET_ID_LINKEDIN")),
            "BRIGHTDATA_DATASET_ID_INDEED": bool(os.environ.get("BRIGHTDATA_DATASET_ID_INDEED")),
            "BRIGHTDATA_BROWSER_ZONE": os.environ.get("BRIGHTDATA_BROWSER_ZONE", "(not set)"),
            "DEMO_MODE": _DEMO_MODE,
        },
    }


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


@app.get("/api/debug/html", dependencies=[Depends(_require_demo_secret)])
async def debug_html(url: str = "https://www.jobstreet.com.my/en/job-search/data-analyst-jobs/in-malaysia/") -> dict:
    """
    Fetch URL via Web Unlocker and report what structured data is present.
    Use to diagnose __NEXT_DATA__ field names and JSON-LD presence.
    Uses Scraping Browser if configured (executes JS), else Web Unlocker.
    """
    html = await _fetch_with_browser(url)
    if not html:
        return {"status": "error", "message": "Browser/Unlocker returned empty"}

    # JSON-LD blocks
    jsonld_blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    )
    jsonld_types: list[str] = []
    for b in jsonld_blocks:
        try:
            d = json.loads(b.strip())
            items = d if isinstance(d, list) else [d]
            for item in items:
                if isinstance(item, dict):
                    jsonld_types.append(item.get("@type", "unknown"))
        except Exception:
            pass

    # __NEXT_DATA__ — show top-level keys and first job-like object if found
    nextdata_info: dict = {}
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
    if m:
        raw = m.group(1).strip()
        nextdata_info["size_bytes"] = len(raw)
        try:
            nd = json.loads(raw)
            nextdata_info["top_keys"] = list(nd.keys())[:10]
            pp = (nd.get("props") or {}).get("pageProps") or {}
            nextdata_info["pageProps_keys"] = list(pp.keys())[:20] if isinstance(pp, dict) else []
            # Find job arrays
            job_items = _find_job_objects_in_json(nd)
            nextdata_info["job_items_found"] = len(job_items)
            if job_items:
                nextdata_info["first_job_keys"] = list(job_items[0].keys())[:20]
                nextdata_info["first_job_sample"] = {
                    k: job_items[0][k]
                    for k in list(job_items[0].keys())[:8]
                }
        except Exception as exc:
            nextdata_info["parse_error"] = str(exc)

    # Text at multiple offsets — find where job listings actually start
    def _slice(start: int, length: int = 8000) -> str:
        chunk = html[start:start + length]
        t = re.sub(r"<[^>]+>", " ", chunk)
        return re.sub(r"\s+", " ", t).strip()[:400]

    return {
        "status": "ok",
        "url": url,
        "html_bytes": len(html),
        "jsonld_blocks": len(jsonld_blocks),
        "jsonld_types": jsonld_types,
        "nextdata": nextdata_info,
        "text_at_5k":   _slice(5_000),
        "text_at_30k":  _slice(30_000),
        "text_at_80k":  _slice(80_000),
        "text_at_150k": _slice(150_000),
        "text_at_250k": _slice(250_000),
        "text_at_350k": _slice(350_000),
        "text_at_450k": _slice(450_000),
    }


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
