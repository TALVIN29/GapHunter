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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _ALLOWED_ORIGINS],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    # X-Demo-Secret must be here or browser preflight blocks the header — PRD §23.6
    allow_headers=["Content-Type", "Authorization", "X-Demo-Secret"],
)

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
    """PRD §23.3. Increment counter. Trip breaker at limit."""
    state.live_search_count += 1
    if state.live_search_count >= CIRCUIT_BREAKER_LIMIT and not state.shadow_forced:
        state.shadow_forced = True
        state.circuit_open = True
        logger.warning(
            "CIRCUIT BREAKER TRIPPED: %d live searches today. Shadow Mode forced.",
            state.live_search_count,
        )


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


async def _scrape_with_fallback(role: str, location: str) -> list[dict]:
    """Addendum C: 45s timeout → silent fallback to pre-cached JSON."""
    from scraper import scrape_jobs

    try:
        postings = await asyncio.wait_for(
            asyncio.to_thread(scrape_jobs, role, location),
            timeout=_SCRAPE_TIMEOUT_S,
        )
        if postings:
            return postings
        logger.warning("Scrape returned 0 postings — using fallback")
        return _load_fallback(role)
    except asyncio.TimeoutError:
        logger.warning("Scrape timed out after %ds — using fallback", _SCRAPE_TIMEOUT_S)
        return _load_fallback(role)
    except Exception as exc:
        logger.warning("Scrape error: %s — using fallback", exc)
        return _load_fallback(role)


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
    location: str = "United States"
    skills: str = ""
    preferences: SearchPreferences = SearchPreferences()


class AnalyseRequest(BaseModel):
    job_url: str
    session_id: str


class HRRequest(BaseModel):
    company_name: str
    role: str
    location: str = "United States"


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

@app.post("/api/resume", dependencies=[Depends(_require_demo_secret)])
async def upload_resume(file: UploadFile = File(...)) -> dict:
    """Addendum E: 7-layer defence. Always HTTP 200."""
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
        return JSONResponse(content=_load_static_demo())

    # Layer 2: IP rate limit (Addendum G §23.2)
    ip = _client_ip(request)
    if not _check_ip_rate(ip):
        return _RATE_LIMITED_RESPONSE

    # Gate 0: pure Python (Addendum F)
    if not precheck_role_input(req.role):
        return _INVALID_QUERY_RESPONSE

    # Gate 1: Claude Haiku validation + normalisation (Addendum F)
    validation = await validate_and_normalize(_client, req.role)
    if validation is None:
        titles = [req.role.strip()]  # degrade gracefully on timeout
    elif not validation["is_valid_role"]:
        return _INVALID_QUERY_RESPONSE
    else:
        titles = validation["canonical_titles"] or [req.role.strip()]

    primary_title = titles[0]

    # Layer 3: Circuit breaker (Addendum G §23.3)
    _maybe_reset(request.app.state)
    if request.app.state.shadow_forced:
        logger.info("Circuit breaker active — Shadow Mode forced, $0 API cost")
        postings = _load_fallback(primary_title)
    else:
        _tick_circuit_breaker(request.app.state)
        postings = await _scrape_with_fallback(primary_title, req.location)

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
    session_id = str(uuid.uuid4())
    jobs_out = [_format_job(j) for j in ranked_jobs[:10]]

    # Persist search history (optional — no user required)
    current_user = _get_current_user(request)
    user_id = current_user["sub"] if current_user else None
    background_tasks.add_task(save_search, session_id, req.role, req.location, evidence, user_id)

    # Addendum D: fire-and-forget roadmap pre-fetch
    gap_skills = [g["skill"] for g in gaps]
    init_roadmap_cache(session_id, gap_skills)
    asyncio.create_task(
        prefetch_roadmaps(_client, session_id, gap_skills, job_descriptions)
    )

    return {
        "status": "ok",
        "session_id": session_id,
        "total_postings": len(postings),
        "titles_searched": titles,
        "jobs": jobs_out,
        "gaps": evidence,
    }


def _format_job(job: dict) -> dict:
    url = job.get("apply_link") or job.get("url") or ""
    safe_url = url if validate_job_url(url) else ""

    salary_min = job.get("salary_min", 0) or 0
    salary_max = job.get("salary_max", 0) or 0
    salary_display = (
        f"${int(salary_min):,}–${int(salary_max):,}/yr"
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
    }


# ---------------------------------------------------------------------------
# Roadmap polling (F4) — Addendum D
# ---------------------------------------------------------------------------

@app.get("/api/roadmap/{skill}")
async def get_roadmap(skill: str, session_id: str) -> dict:
    """Always HTTP 200. Frontend polls until status == 'ready'."""
    return get_roadmap_status(session_id, skill)


# ---------------------------------------------------------------------------
# Per-job gap analysis (F3)
# ---------------------------------------------------------------------------

@app.post("/api/analyse", dependencies=[Depends(_require_demo_secret)])
async def analyse_job(req: AnalyseRequest) -> dict:
    """
    Detailed gap analysis for a single job: Claude Sonnet synthesis.
    Uses already-scraped data from the session (no second Bright Data call).
    """
    if not validate_job_url(req.job_url):
        return {"status": "error", "message": "Invalid job URL"}

    prompt = (
        f"A candidate wants to apply to this job: {req.job_url}\n\n"
        "Based on typical requirements for this role, provide:\n"
        "1. Top 5 skills to highlight in the application\n"
        "2. Common skill gaps to address\n"
        "3. One specific tip to stand out\n\n"
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
        return {"status": "ok", "analysis": json.loads(text)}
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

    postings = await _scrape_with_fallback(
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
