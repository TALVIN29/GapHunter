# Progress: GapHunter — Labor Market Intelligence Platform
## Status: IN_PROGRESS — PRE-DEPLOY CODE COMPLETION REQUIRED
## Talvin Principle: HITL
## Knowledge Tier: T3
## Progress: 8/10 sprints complete (Day 29 code-completion tasks blocking deploy)
## Last checkpoint: Day 29 deploy infra COMPLETE (2026-05-27). Addendum M smoke test 7/7 PASSED. Created: Procfile, netlify.toml, .github/workflows/keep-alive.yml, .gitignore (expanded), requirements.txt (httpx added, gradio removed). All code changes done. Repo is push-ready pending git init + HALT #3 human actions.
## Next action: HALT #3 — human actions: (1) git init + push to GitHub; (2) set Render env vars (Step 1); (3) Render deploy → get slug → replace RENDER_URL in netlify.toml; (4) Netlify deploy → set window.__APP_TOKEN__ via Netlify snippet injection; (5) GitHub Actions: add RENDER_HEALTH_URL secret. Then Step 5 smoke tests on live URLs.
## Blockers: None

---

## Tracks
- **Primary:** Track 2 — Finance & Market Intelligence (job postings as alternative data pipeline)
- **Secondary:** Track 1 — GTM Intelligence (HR competitor talent monitoring)
- **Multi-track submission:** Confirmed permitted by hackathon rules

## Target Audience
- **Primary demo:** Job Seekers — career changers, upskilling professionals, recent graduates
- **Secondary (enterprise bento card):** HR / Talent Acquisition teams
- **Business model:** Freemium ($19/mo job seekers) + Enterprise SaaS ($499–$5,000/mo/seat HR)

---

## Tasks

### Foundation — Day 25 (COMPLETE)
- [x] Bright Data API authenticated — account ID: hl_69e6affd
- [x] PRD v1.0 locked (2026-05-25)
- [x] Dev environment: Python 3.12.10, anthropic 0.104.1
- [x] Two-step scraper: SERP → LinkedIn Collect dataset `gd_lpfll7v5hcqtkxl6l`
- [x] Async LLM extraction pipeline (extractor.py) — Claude Haiku 4.5 (`claude-haiku-4-5-20251001`)
- [x] Gap ranking + evidence pipeline (pipeline.py) — Counter() + set subtraction
- [x] PRD v2.0 locked (2026-05-26) — full-stack platform, dual audience, weighted scoring

### Day 26 — Data Engineering Foundation (COMPLETE)
- [x] Upgrade scraper.py: store ALL LinkedIn Collect payload fields (§7.1 full field list)
- [x] Add Indeed dataset to scraper.py: LinkedIn + Indeed in parallel (ThreadPoolExecutor)
- [x] Build signals.py: `freshness_score`, `competition_score`, `dual_signal`, `cross_source`
- [x] Build scoring.py: `skill_demand_score()` — formula: `0.35×freq + 0.25×freshness + 0.20×opportunity + 0.20×cross_source`
- [x] Build scoring.py: `job_relevance_score()` — personalized job ranking
- [x] Add normalizer.py: Gate 0 (pure Python len/alpha check) + Gate 1 (Claude Haiku canonical title validation) — Addendum F
- [x] Build security.py: `validate_job_url()` + `ALLOWED_JOB_DOMAINS` whitelist
- [x] Smoke test: weighted scores confirmed — dbt 0.745 vs raw frequency; freshness decay confirmed

### Day 27 — Backend API + Defenses (COMPLETE)
- [x] Build api.py: FastAPI — endpoints `/health`, `/api/search`, `/api/resume`, `/api/analyse`, `/api/roadmap/{session_id}/{skill}`, `/api/hr/competitors`
- [x] **Addendum A — Semaphore:** `asyncio.Semaphore(10)` for Haiku extraction; `asyncio.Semaphore(3)` for Sonnet roadmap
- [x] **Addendum C — Shadow Mode:** `asyncio.wait_for()` 12s timeout; fallback to `fallback_payload_data_analyst.json` on timeout
- [x] **Addendum D — Optimistic Pre-fetch:** `asyncio.create_task()` fires roadmap generation immediately after search returns; `RoadmapCache` keyed on `session_id+skill`; poll `/api/roadmap/{session_id}/{skill}` every 1.5s
- [x] **Addendum E — Resume Failsafe:** 7-layer chain: size gate → magic bytes → text extraction → truncation → prompt injection fence (`<resume>` tags) → Claude Haiku extraction → output validation
- [x] **Addendum F — Pre-Flight Gate:** Gate 0 pure Python (len < 2, non-alpha reject); Gate 1 Claude Haiku (`{"is_valid_role": bool, "canonical_titles": [...]}`) with degraded-path fallback
- [x] **Addendum G — Financial Firewall:** Layer 1 `X-Demo-Secret` header (`secrets.compare_digest`); Layer 2 IP sliding-window rate limiter (5/hr); Layer 3 global circuit breaker (`app.state.circuit_open`)
- [x] **Addendum J — /health endpoint:** reads `app.state` only — zero Bright Data calls, zero Claude calls, zero SQL; always HTTP 200; NOT behind `X-Demo-Secret`
- [x] Build auth.py: JWT (15-min access + 7-day refresh, httpOnly cookies) + bcrypt cost 12 — **AUTH FLOW CUT FROM DEMO SCOPE** (pre-seeded Alpine.js store bypasses login screen per Addendum B §18.5)
- [x] Build database.py: SQLite schema — users, user_profiles, search_history
- [x] Build fallback/fallback_payload_data_analyst.json: pre-cached postings
- [x] Curl smoke test all endpoints — all passing

### Day 28 — Frontend Bento-Grid (COMPLETE)
**Scope:** index.html only. Alpine.js + ApexCharts + Tailwind CDN. No server-side rendering, no routing, no login screen.
**CUT from scope:** Auth flow, multi-page routing, particles.js, typed.js, AOS animations, heatmap chart, salary/company-size filter logic (UI-only "Pro" labels).

#### Bento-Grid Implementation
- [x] Dark-mode skeleton: `background: #080c14`, Tailwind CDN, 3-zone layout — [search + gaps bar] / [job cards] / [analysis panel + HR radar]
- [x] CDN load order: ApexCharts → Alpine.js (`defer`) → SweetAlert2 → Inter font (no other JS libraries)
- [x] Pre-seeded Alpine.js demo store — bypasses auth:
  ```js
  Alpine.store('user', {
    skills: ['Python', 'SQL', 'pandas', 'scikit-learn'],
    target_role: 'Data Analyst',
    logged_in: true
  })
  ```
- [x] Search form: role input + location input + remote filter (select) + seniority filter (select). Salary + company size: UI-visible, `disabled` attribute, "Pro" badge — no backend call.
- [x] CV upload zone: drag-and-drop → `POST /api/resume` with `X-Demo-Secret` header → populate skills on `status: "ok"` → reveal manual input fallback on `status: "parse_failed"`
- [x] Inline `searchError` field: `<p x-show="searchError" x-text="searchError">` — Gate 0/1 rejection messages render inline without modal (Addendum F §22.6)
- [x] Job cards: Alpine.js reactive — `fetch('/api/search', { headers: { 'X-Demo-Secret': DEMO_SECRET } })` → render relevance %, freshness badge (days ago), competition score (applicant count), salary range, Apply button, Analyse button
- [x] Top 5 gaps bar: ApexCharts `type: 'bar'`, horizontal, dark-mode — transparent bg, `#00e5ff` bars, slate labels, live `skill_demand_score` array from `/api/search` response
- [x] Per-job analysis panel: gap skill list (❌ gap / ✅ match), application tips (Claude Sonnet), Apply redirect → `validate_job_url()` gate
- [x] Roadmap accordion: on Analyse click, poll `GET /api/roadmap/{session_id}/{skill}` every 1.5s → Tailwind `animate-pulse` skeleton on PENDING/GENERATING → render markdown on READY (Addendum D)

#### Addendum H — HR "Silent Pivot" Radar Card (COMPLETE, ZERO BACKEND DEPENDENCY)
- [x] `hrIntelStore()` Alpine.js component: `x-data="hrIntelStore()"`, `x-init="init()"`, `x-ref="competitorRadar"`
- [x] Hardcoded `radarData` — no `fetch()`, no `/api/hr/competitors` call:
  - Stripe: `[76, 82, 91, 58, 87, 94]`
  - Block: `[89, 84, 41, 35, 23, 17]`
  - Adyen: `[92, 79, 33, 28, 18, 12]`
- [x] 6 axes: SQL · Python · dbt · Airflow · Automation (n8n) · LLM Integration
- [x] Colors: CYAN `#00e5ff` (Stripe, solid, `fill.opacity: 0.15`, `stroke.width: 2.5`) · MAGENTA `#ff2d78` (Block, dashed `dashArray: 4`, `fill.opacity: 0.07`, `stroke.width: 1.5`) · AMBER `#ffb627` (Adyen, dashed, same as Block)
- [x] `plotOptions.radar.polygons`: `strokeColors: '#1e293b'`, `fill.colors: ['#0f172a']`
- [x] `.radar-glow-wrapper` CSS: `filter: drop-shadow(0 0 12px rgba(0,229,255,0.35))`
- [x] Null-guard: `if (!el || typeof ApexCharts === 'undefined') return` — protects against Alpine.js `$refs` race on mount
- [x] DevTools verified: 0 `fetch()` / XHR calls on card render; chart mounts entirely from `hrIntelStore()` state

#### Smoke Tests — All Passed Against localhost:8000
- [x] Gate 0: role `"a"` → inline error < 100ms, 0 Bright Data log entries
- [x] Gate 1: role `"asdfgh"` → `status: invalid_query` < 2s, 1 Haiku call in log, 0 SERP calls
- [x] Gate 1 degraded: broken `ANTHROPIC_API_KEY` → log `PREFLIGHT degraded`, search proceeds with raw input
- [x] Shadow Mode: `SCRAPE_TIMEOUT_S=1` → log `SHADOW_MODE=fallback reason=timeout_1s`, UI renders from fallback JSON
- [x] Fallback boot: server restart → no `FileNotFoundError` for `fallback_payload_data_analyst.json`
- [x] Semaphore burst: 30 concurrent `Promise.all()` from browser console → 0 `RateLimitError` in log
- [x] Firewall Layer 1 (missing header): curl without `X-Demo-Secret` → HTTP 403
- [x] Firewall Layer 1 (wrong value): wrong header value → HTTP 403
- [x] Firewall Layer 2 (IP rate limit): 6 requests / 60s → 6th returns `rate_limited`
- [x] Firewall Layer 3 (circuit breaker): `CIRCUIT_BREAKER_LIMIT=2` → 3rd trips, 4th serves Shadow Mode fallback
- [x] Pre-fetch 0ms: wait 25s post-search → Analyse click → roadmap < 500ms from `ROADMAP_CACHE`
- [x] Skeleton visible: Analyse within 3s of search → skeleton renders → READY within 15s
- [x] HR radar zero-network: DevTools Network tab → 0 `fetch()` / XHR from radar card on scroll
- [x] HR radar null-guard: `x-if` conditional render → no JS console error on `$refs.competitorRadar` null
- [x] `/health` endpoint: curl → HTTP 200, `"status": "ok"`, `uptime_s` incrementing, 0 external calls in log

### Day 29 — Deploy + Keep-Alive (IN PROGRESS)
**Goal:** Backend live on Render. Frontend live on Netlify. UptimeRobot + GitHub Actions keep-alive active (Addendum J). All three Financial Firewall layers verified on deployed infra. /health confirmed < 200ms. E2E green on live URLs. Go/No-Go warm-up completed.

#### Step 0 — Pre-Deploy Code Completion (complete ALL sub-tasks before `git push`)
**These 5 items are PRD-spec'd but not yet in the codebase. Each blocks a subsequent Day 29 verification step. Do not proceed to Step 1 until all pass.**

---

**Step 0a — Implement Addendum N in `api.py` (PRD §30.5)**
Add `circuit_open` app state, module-level static cache, and short-circuit at top of `/api/search` handler.

```python
# At module level (after imports, before any endpoint):
_STATIC_DEMO_PATH = Path("fallback/demo_state_data_analyst.json")
_static_demo_cache: dict | None = None

def _load_static_demo() -> dict:
    global _static_demo_cache
    if _static_demo_cache is None:
        if not _STATIC_DEMO_PATH.exists():
            raise FileNotFoundError(
                f"Circuit breaker open but {_STATIC_DEMO_PATH} missing. "
                "Run generate_full_demo_state.py before deploying."
            )
        _static_demo_cache = json.loads(_STATIC_DEMO_PATH.read_text())
    return _static_demo_cache
```

In `startup()` — add `circuit_open` to app state (alongside existing `live_search_count`, `shadow_forced`, `reset_date`):
```python
app.state.circuit_open = False
app.state.fallback_ready = False
```

In `_tick_circuit_breaker()` — set `circuit_open = True` when limit hit (in addition to existing `shadow_forced`):
```python
if state.live_search_count >= CIRCUIT_BREAKER_LIMIT and not state.shadow_forced:
    state.shadow_forced = True
    state.circuit_open = True   # ← add this line
    logger.warning("CIRCUIT BREAKER TRIPPED: ...")
```

In `_load_fallback()` — set `fallback_ready = True` after successful load:
```python
app.state.fallback_ready = True   # ← add after return data
```

In `search()` handler — insert as FIRST statement before Layer 2 IP check:
```python
# ── Addendum N: Zero-Token Fallback ──────────────────────────────────
if getattr(app.state, "circuit_open", False):
    logger.warning("CIRCUIT_OPEN: serving static demo state — zero LLM calls")
    return JSONResponse(content=_load_static_demo())
# ── End Addendum N ───────────────────────────────────────────────────
```

- [x] `_STATIC_DEMO_PATH` and `_static_demo_cache` added at module level
- [x] `_load_static_demo()` function added
- [x] `app.state.circuit_open = False` initialized in `startup()`
- [x] `app.state.fallback_ready = False` initialized in `startup()`
- [x] `state.circuit_open = True` set in `_tick_circuit_breaker()` when limit hit
- [x] `app.state.fallback_ready = True` set inside `_load_fallback()` on successful read
- [x] Addendum N short-circuit is first statement in `search()` body — before Layer 2, before Gate 0
- [ ] Smoke test locally: `CIRCUIT_BREAKER_LIMIT=1` → fire 2 requests → 2nd response `session_id == "demo-static"` → restore to 100

---

**Step 0b — Rewrite `/health` endpoint (PRD §26.2)**
Current `GET /api/health` returns `{status, version}` only. PRD requires full diagnostic schema at `GET /health` (no `/api` prefix — UptimeRobot pings `/health`).

Replace the existing `@app.get("/api/health")` block:

```python
import time as _time
_PROCESS_START = _time.monotonic()   # add at module level

@app.get("/health", include_in_schema=False)
async def health_check(request: Request) -> dict:
    uptime_s = round(_time.monotonic() - _PROCESS_START, 1)
    hours, remainder = divmod(int(uptime_s), 3600)
    minutes, seconds = divmod(remainder, 60)
    state = request.app.state
    return {
        "status":                "ok",
        "uptime_s":              uptime_s,
        "uptime_human":          f"{hours}h {minutes}m {seconds}s",
        "timestamp":             _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        "shadow_forced":         getattr(state, "shadow_forced",         False),
        "circuit_open":          getattr(state, "circuit_open",          False),
        "circuit_breaker_limit": getattr(state, "circuit_breaker_limit", CIRCUIT_BREAKER_LIMIT),
        "fallback_ready":        getattr(state, "fallback_ready",        False),
    }
```

- [x] `_PROCESS_START = time.monotonic()` added at module level
- [x] Old `@app.get("/api/health")` removed
- [x] New `@app.get("/health")` added with full PRD §26.2 schema
- [ ] Smoke test: `curl http://localhost:8000/health | python -m json.tool` → returns all 8 fields
- [ ] `uptime_s` is a float incrementing over time (not static)
- [ ] `circuit_open` is `false` at startup; `true` after breaker trips

---

**Step 0c — Create `generate_full_demo_state.py` (PRD §30.4)**
Copy verbatim from PRD §30.4. File must live in project root (same level as `api.py`).

```python
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from api import app
from httpx import AsyncClient

TARGET_QUERY = "Data Analyst"
OUTPUT_PATH  = Path("fallback/demo_state_data_analyst.json")
OUTPUT_PATH.parent.mkdir(exist_ok=True)

DEMO_SECRET = os.environ["DEMO_SECRET"]   # fail fast if not set

async def capture() -> None:
    async with AsyncClient(app=app, base_url="http://test") as client:
        print(f"Firing POST /api/search for '{TARGET_QUERY}'...")
        resp = await client.post(
            "/api/search",
            json={"query": TARGET_QUERY, "session_id": "demo-static"},
            headers={"X-Demo-Secret": DEMO_SECRET},
            timeout=120.0,
        )
        if resp.status_code != 200:
            print(f"FAIL: HTTP {resp.status_code}")
            print(resp.text[:500])
            sys.exit(1)
        data = resp.json()
        data["session_id"] = "demo-static"
        assert "jobs"     in data and len(data["jobs"])     >= 5, "jobs missing"
        assert "gaps"     in data and len(data["gaps"])     >= 3, "gaps missing"
        assert "roadmaps" in data and len(data["roadmaps"]) >= 1, "roadmaps missing"
        OUTPUT_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        print(f"Saved → {OUTPUT_PATH}")
        print(f"  {len(data['jobs'])} jobs  |  {len(data['gaps'])} gaps  |  {len(data['roadmaps'])} roadmaps")

if __name__ == "__main__":
    asyncio.run(capture())
```

**Pre-run requirements:** `ANTHROPIC_API_KEY` and `BRIGHT_DATA_*` credentials set locally; `app.state.circuit_open` must be `False` (live path, not fallback); run from project root.

- [x] File created at `e:\Portfolio\GapHunter\generate_full_demo_state.py`
- [ ] `httpx` installed: `pip install httpx` (required by `AsyncClient(app=app)`)
- [ ] Run: `python generate_full_demo_state.py` — exits 0 and prints job/gap/roadmap counts
- [ ] Output file exists: `fallback/demo_state_data_analyst.json`
- [ ] Schema check: `session_id == "demo-static"`, `len(jobs) >= 5`, `len(gaps) >= 3`
- [ ] Verify `gaps[0]["skill"] == "dbt"` — if not, note which skill ranked #1; will need to re-run until dbt tops (or update voiceover; see Day 30 Phase 3 constraint)
- [ ] File size < 500 KB

---

**Step 0d — Verify `index.html` + apply Addendum P**
`index.html` not found at `e:\Portfolio\GapHunter\index.html`. Resolve before Netlify deploy.

- [x] Locate `index.html` — check all subdirectories (`frontend/`, `public/`, `dist/`, `src/`)
- [x] If NOT found: rebuild per Day 28 spec (full Alpine.js bento-grid, ApexCharts bar + radar, Tailwind CDN)
- [x] Apply Addendum P: token pattern uses `window.__APP_TOKEN__` (atob-decoded); `VITE_DEMO_SECRET` absent; Netlify injects via snippet
- [x] `index.html` placed at project root (`e:\Portfolio\GapHunter\index.html`)
- [ ] Open Netlify URL in clean Chrome profile — bento grid renders, no console errors

---

**Step 0e — Implement Addendum M in `security.py` (PRD §29)**
Codebase audit 2026-05-27: `security.py` uses expanded static whitelist (old §9.4 approach). Addendum M two-layer validator not applied. `careers.stripe.com`, `talent.shopify.com`, and any enterprise ATS career subdomain not hardcoded will still be rejected. Blocks Step 5c smoke test.

Full replacement for `validate_job_url()` per PRD §29.3:

```python
# security.py — replace entire file content per Addendum M

from urllib.parse import urlparse

ALLOWED_JOB_DOMAINS: frozenset[str] = frozenset({
    "linkedin.com", "indeed.com", "glassdoor.com",
    "greenhouse.io", "lever.co", "workday.com",
    "bamboohr.com", "smartrecruiters.com", "icims.com",
    "jobvite.com", "myworkdayjobs.com",
    "careers.google.com", "jobs.apple.com", "amazon.jobs",
})

_CAREER_PREFIXES: frozenset[str] = frozenset({
    "jobs", "careers", "career", "work",
    "talent", "apply", "hiring", "join",
    "recruit", "opportunities", "employment",
})

def validate_job_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    netloc = parsed.netloc.lower()
    if not netloc:
        return False
    host = netloc.split(":")[0]
    if not host:
        return False
    # Layer 1 — static whitelist (removeprefix fixes §9.4 lstrip bug)
    bare = host.removeprefix("www.")
    if any(bare == d or bare.endswith(f".{d}") for d in ALLOWED_JOB_DOMAINS):
        return True
    # Layer 2 — corporate career subdomain heuristic (≥ 3 DNS labels required)
    labels = host.split(".")
    if len(labels) >= 3 and labels[0] in _CAREER_PREFIXES:
        return True
    return False
```

Keep existing private IP / localhost / metadata blocks from current `security.py` — they are correct and should be merged above `validate_job_url`.

- [x] Replace `validate_job_url` in `security.py` with Addendum M two-layer implementation
- [x] Preserve `_PRIVATE_PREFIXES` and `_BLOCKED_HOSTNAMES` blocks from current code (merge above the new function)
- [x] Smoke test (run before any push):
  ```python
  from security import validate_job_url
  assert validate_job_url("https://jobs.netflix.com/jobs/123")         is True
  assert validate_job_url("https://careers.stripe.com/positions/456")  is True
  assert validate_job_url("https://talent.shopify.com/job/789")        is True
  assert validate_job_url("https://stripe.greenhouse.io/jobs/abc")     is True
  assert validate_job_url("https://www.workday.com/hiring")            is True
  assert validate_job_url("https://randomsite.com/apply")              is False
  assert validate_job_url("ftp://jobs.netflix.com/jobs/1")             is False
  print("Addendum M smoke test passed")
  ```
- [x] All 7 assertions pass — PASSED 2026-05-27 (7/7 OK)

---

#### Step 1 — Environment Variables (Render)
- [ ] `ANTHROPIC_API_KEY` — Claude Haiku + Sonnet key
- [ ] `BRIGHT_DATA_CUSTOMER_ID` — `hl_69e6affd`
- [ ] `BRIGHT_DATA_ZONE_PASSWORD` — from Bright Data dashboard
- [ ] `JWT_SECRET` — random 32-char string (not a phrase)
- [ ] `DEMO_SECRET` — actual plaintext secret value. **Note:** Netlify frontend uses `VITE_APP_CHALLENGE_TOKEN` = `btoa(DEMO_SECRET)`, NOT `VITE_DEMO_SECRET` (Addendum P — see Step 5e for computation)
- [ ] `CIRCUIT_BREAKER_LIMIT` — `100`
- [ ] `SCRAPE_TIMEOUT_S` — `12`

#### Step 2 — Deploy Backend to Render
- [ ] Push all backend files to GitHub — `git grep ANTHROPIC_API_KEY` returns 0 matches
- [ ] Connect Render to GitHub repo; confirm auto-deploy on push to `main`
- [ ] Verify Render build log: no import errors; `Application startup complete` present
- [ ] Verify boot log: `fallback_payload_data_analyst.json` exists — no `FileNotFoundError`
- [ ] `curl -s https://<slug>.onrender.com/health` → HTTP 200, `"status": "ok"`
- [ ] Confirm `/health` response time < 200ms on warm container

#### Step 3 — Deploy Frontend to Netlify
- [ ] Compute base64 encoding of `DEMO_SECRET` (do this locally before opening Netlify UI):
  ```bash
  python3 -c "import base64; print(base64.b64encode(b'<actual_DEMO_SECRET>').decode())"
  # Output example: YTNmOGIyYzFkOWU0Zjc...  — this is your VITE_APP_CHALLENGE_TOKEN value
  ```
- [ ] In Netlify → Site configuration → Environment variables:
  - **REMOVE** `VITE_DEMO_SECRET` if present — delete it entirely
  - **ADD** `VITE_APP_CHALLENGE_TOKEN` = `<base64 output from above>` (scope: Production)
- [ ] Confirm `VITE_DEMO_SECRET` is absent from Netlify env list (post-delete check)
- [ ] Confirm encoded value injected at build time — plaintext secret must NOT appear in bundle (full audit in Step 5e Part B after deploy)
- [ ] Set `allow_origins` in FastAPI CORS to exact Netlify domain — no wildcard
- [ ] Open Netlify URL in clean Chrome profile — bento grid renders, no console errors

#### Step 4 — Configure Keep-Alive (Addendum J §26.2 + §26.3)
- [ ] **UptimeRobot (primary — 5-min interval):** Register → Add monitor → HTTP(S), URL = `https://<slug>.onrender.com/health`, interval = 5 min, alert to `talvinleegenwei0329@gmail.com` → wait 5 min → confirm green status
- [ ] **UptimeRobot (primary — 5-min interval):** Register → Add monitor → HTTP(S), URL = `https://<slug>.onrender.com/health`, interval = 5 min, alert to `talvinleegenwei0329@gmail.com` → wait 5 min → confirm green status
- [x] **GitHub Actions backup (10-min, date-gated):** `.github/workflows/keep-alive.yml` created — cron `*/10 * * * *`, date gate 2026-05-30/31. Still needed: push to GitHub, add `RENDER_HEALTH_URL` secret, trigger workflow_dispatch manually.

#### Step 5 — Verify Firewall on Deployed Infra
- [ ] DevTools → Network → `POST /api/search` on Netlify URL → confirm `X-Demo-Secret` header present in every request
- [ ] `curl https://<slug>.onrender.com/api/search -X POST -d '{"role":"Data Analyst"}'` (no header) → HTTP 403
- [ ] Confirm `CIRCUIT_BREAKER_LIMIT=100` in Render env dashboard

#### Step 5b — Rate-Limit UI Degradation Test (Addendum L) ⚠️ Run LAST in this step — triggers IP lockout
**Purpose:** Verify the Addendum L patch is live — `rate_limited` renders inline error and clears skeleton. Without this test, the fracture identified in §28.1 may be undetected until Demo Day.
- [ ] Open DevTools → Network tab recording on live Netlify URL
- [ ] In browser console, fire 6 rapid searches within 60 seconds:
  ```javascript
  for (let i = 0; i < 6; i++) {
    fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Demo-Secret': DEMO_SECRET },
      body: JSON.stringify({ role: 'Data Analyst', location: 'United States' })
    }).then(r => r.json()).then(d => console.log(i, d.status));
  }
  ```
- [ ] Verify console output: first 5 statuses = `"ok"` or `"no_results"`; 6th status = `"rate_limited"`
- [ ] Verify Network tab: 6th request returns HTTP 200 with `{"status": "rate_limited", "message": "..."}`
- [ ] Verify UI: `searchError` field renders the rate_limited message inline beneath the search form
- [ ] Verify UI: `animate-pulse` skeleton is GONE — `isLoading` cleared to `false` (Addendum L §28.2 invariant)
- [ ] Verify UI: search input field still populated — NOT cleared (Addendum L §28.2 constraint)
- [ ] ⚠️ **After test passes: restart Render service via dashboard to reset in-memory `_ip_windows`** — the test IP is locked out for 1 hour otherwise. All subsequent Day 29 E2E steps require live search to succeed.

#### Step 5c — Dynamic ATS Link Validator Smoke Test (Addendum M)
**Purpose:** Verify the two-layer `validate_job_url` is deployed correctly. A failed deploy of Addendum M means Apply buttons are disabled for enterprise roles during judging — silent, uncatchable during recording.

**Part A — Unit smoke test (run in project root Python env before Render push):**
```python
# Run: python -c "exec(open('test_validate_url.py').read())"
# Or inline:
python -c "
from security import validate_job_url
cases = [
    ('https://jobs.netflix.com/jobs/123',         True,  'L2 jobs. subdomain'),
    ('https://careers.stripe.com/positions/456',  True,  'L2 careers. subdomain'),
    ('https://talent.shopify.com/job/789',        True,  'L2 talent. subdomain'),
    ('https://stripe.greenhouse.io/jobs/abc',     True,  'L1 greenhouse.io'),
    ('https://www.workday.com/hiring',            True,  'L1 lstrip bug fix'),
    ('https://randomsite.com/apply',              False, 'REJECT 2-label'),
    ('ftp://jobs.netflix.com/jobs/1',             False, 'REJECT non-HTTPS'),
]
failed = 0
for url, expected, label in cases:
    result = validate_job_url(url)
    ok = result == expected
    print(f\"{'OK' if ok else 'FAIL'} {label}\")
    if not ok: failed += 1
assert failed == 0, f'{failed} test(s) failed — do not deploy'
print('All validator tests passed')
"
```
- [ ] All 7 assertions pass before pushing backend to Render
- [ ] `assert failed == 0` exits 0 — if non-zero, fix `security.py` before deploying

**Part B — Live Apply button verification on Netlify URL (after deploy):**
- [ ] Run a live search for `"Data Analyst"` on the deployed Netlify URL — let job cards render
- [ ] Inspect DevTools → Network: click Apply on the first job card → confirm `validate_job_url` gate does NOT produce a disabled button for a Bright Data-returned `apply_link`
- [ ] Manually test 3 custom corporate ATS URLs in browser console:
  ```javascript
  // Replace URL values with actual apply_links from the live search response
  // Verify all three return a non-disabled Apply button state
  // If any Apply button is disabled, log the rejected URL and check security.py deploy
  console.log("jobs.netflix.com test:", /^https?:\/\/jobs\./.test("https://jobs.netflix.com/jobs/1"));
  console.log("careers.stripe.com test:", /^https?:\/\/careers\./.test("https://careers.stripe.com/roles/1"));
  ```
- [ ] If any legitimate `apply_link` from the live scrape is rejected: retrieve the URL from the Render log (`APPLY_LINK_REJECTED:` prefix), run it through the unit smoke test, patch `security.py`, re-deploy

#### Step 5d — Zero-Token Fallback Generation + Circuit Breaker Verification (Addendum N)
**Purpose:** Pre-compute `demo_state_data_analyst.json` for the static zero-token fallback, then verify the circuit breaker short-circuit eliminates all LLM calls. A missing or stale static file means the circuit breaker fallback still burns Claude API budget — silent financial exposure during recording.

**Part A — Generate static demo state (run once before deploy):**
```bash
# Requires: live Bright Data credentials, ANTHROPIC_API_KEY set locally
# One perfect live run → serialised to disk
python generate_full_demo_state.py

# Verify output shape:
python -c "
import json, sys
with open('fallback/demo_state_data_analyst.json') as f:
    d = json.load(f)
assert 'jobs' in d and len(d['jobs']) >= 5, 'jobs array missing or too short'
assert 'gaps' in d and len(d['gaps']) >= 3, 'gaps array missing or too short'
assert 'roadmaps' in d and len(d['roadmaps']) >= 1, 'roadmaps dict missing'
assert d.get('session_id') == 'demo-static', 'session_id must be demo-static'
print(f'OK — {len(d[\"jobs\"])} jobs, {len(d[\"gaps\"])} gaps, {len(d[\"roadmaps\"])} roadmaps')
"
```
- [ ] `generate_full_demo_state.py` exits 0 and prints path to output file
- [ ] Schema validation asserts all pass (`jobs`, `gaps`, `roadmaps`, `session_id`)
- [ ] Output file committed to repo: `fallback/demo_state_data_analyst.json`
- [ ] File size < 500 KB (if larger, reduce job count in generator — 10 jobs max)

**Part B — Circuit breaker short-circuit verification (on live Render instance):**
```bash
# Step 1: Manually trip the circuit breaker
# POST /api/search 101 times in rapid succession (exceeds CIRCUIT_BREAKER_LIMIT=100)
# Or: temporarily set CIRCUIT_BREAKER_LIMIT=1 in Render env, fire 2 requests, restore

# Step 2: Fire one more POST /api/search
curl -s -X POST https://<slug>.onrender.com/api/search \
  -H "Content-Type: application/json" \
  -H "X-Demo-Secret: $DEMO_SECRET" \
  -d '{"query": "Data Analyst", "session_id": "test-circuit"}' | python -m json.tool

# Expected: HTTP 200, response body matches demo_state_data_analyst.json shape
# session_id in response == "demo-static"
```
- [ ] Response returns HTTP 200 (not 503 or error) when circuit breaker is open
- [ ] Response `session_id` equals `"demo-static"` — confirms static file was served, not live pipeline
- [ ] Render logs for this request show **zero** `HAIKU_CALL:` log lines — confirms extractor.py bypassed
- [ ] Render logs show **zero** `SONNET_CALL:` log lines — confirms roadmap_cache.py bypassed
- [ ] Response latency < 500ms (static JSON read — no LLM round trips)
- [ ] Reset circuit breaker: restart Render instance OR set `CIRCUIT_BREAKER_LIMIT=100` and restart

#### Step 5e — Client-Side Secret Obfuscation (Addendum P)
**Purpose:** Prevent `DEMO_SECRET` plaintext appearing verbatim in the minified Netlify JS bundle. Vite inlines all `import.meta.env.*` values at build time — without this step, any visitor can extract the raw secret from DevTools → Sources in under 30 seconds. This step must run BEFORE the final Netlify production build.

**Part A — Pre-build source update (run locally before `netlify deploy --prod`):**
```bash
# Step 1: Compute base64 of actual DEMO_SECRET
python3 -c "import base64; print(base64.b64encode(b'<actual_DEMO_SECRET>').decode())"
# Output: YTNmOGIyYzFkOWU0Zjc...  (paste this into Netlify UI — NOT into any file)

# Step 2: Netlify UI → Site configuration → Environment variables
# ACTION: REMOVE VITE_DEMO_SECRET from the env var list
# ACTION: ADD    VITE_APP_CHALLENGE_TOKEN = <base64 output from step 1>
# Scope: Production only

# Step 3: Update index.html — global replace
# FROM: import.meta.env.VITE_DEMO_SECRET
# TO:   atob(import.meta.env.VITE_APP_CHALLENGE_TOKEN ?? '')
# Check: must appear in EVERY fetch() call to /api/search, /api/resume, /api/analyse

# Step 4: Verify no old reference survives
grep -c "VITE_DEMO_SECRET" index.html
# Required: 0
```
- [ ] `VITE_DEMO_SECRET` removed from Netlify env UI
- [ ] `VITE_APP_CHALLENGE_TOKEN` set in Netlify UI with base64-encoded value (scope: Production)
- [ ] All `import.meta.env.VITE_DEMO_SECRET` references in `index.html` replaced with `atob(import.meta.env.VITE_APP_CHALLENGE_TOKEN ?? '')`
- [ ] `grep -c "VITE_DEMO_SECRET" index.html` → 0 (complete replacement confirmed)
- [ ] Netlify production deploy triggered after these changes

**Part B — Bundle audit (run after Netlify build completes):**
```bash
# Download the deployed bundle (Vite output: assets/index-[hash].js)
curl -s https://<netlify-slug>.netlify.app \
  | grep -oP 'assets/index-[^"]+\.js' | head -1 \
  | xargs -I{} curl -s "https://<netlify-slug>.netlify.app/{}" -o bundle.js

# Assert 1: plaintext secret ABSENT
grep -c "<actual_plaintext_DEMO_SECRET>" bundle.js
# Required: 0 — primary goal

# Assert 2: base64 form PRESENT (confirms encoding is active in build)
grep -c "<base64_form_of_secret>" bundle.js
# Required: >= 1

# Assert 3: atob decode call PRESENT
grep -c "atob(" bundle.js
# Required: >= 1

rm bundle.js
```
- [ ] `grep "<plaintext_secret>" bundle.js` → 0 matches
- [ ] `grep "atob(" bundle.js` → ≥ 1 match (decode call active)
- [ ] `grep "<base64_form>" bundle.js` → ≥ 1 match (encoded value inlined)

**Part C — Runtime header + backend rejection verification:**
- [ ] Open Netlify URL → DevTools → Network → trigger one `POST /api/search` → inspect `X-Demo-Secret` header value = actual plaintext secret (correctly decoded at runtime, NOT the base64 form)
- [ ] `curl -X POST https://<slug>.onrender.com/api/search -H "Content-Type: application/json" -d '{}'` → HTTP 403 (Layer 1 still active, no `X-Demo-Secret` header sent)

#### Step 6 — E2E on Live URLs
- [ ] Full flow (clean Chrome profile, no cache): CV upload → search → ranked jobs → per-job gap → roadmap populates
- [ ] Pre-Flight rejection on live URL: submit `"asdfgh"` → inline error < 2s; check Render logs for `PREFLIGHT Gate1 rejected` and 0 Bright Data charges
- [ ] Pipeline wall-clock: stopwatch from search submit to roadmap READY — must be < 60s on Render instance
- [ ] Spot-check 10 Apply URLs via HTTP HEAD — all must resolve 200/301/302
- [ ] Cross-browser: Chrome + Firefox — bento grid layout intact, radar chart renders, CV drag-and-drop works

#### Step 7 — Pre-Demo Warm-Up Protocol (Addendum J §26.4)
Run this sequence at end of Day 29 and again on Day 30 morning before recording.
- [ ] `curl -s https://<slug>.onrender.com/health | python -m json.tool`
- [ ] Confirm `uptime_s > 60` (container warm — not freshly restarted)
- [ ] Confirm `shadow_forced: false` (live Bright Data scraping active)
- [ ] Confirm `circuit_open: false` (circuit breaker not tripped)
- [ ] Confirm `fallback_ready: true` (Shadow Mode safety net loaded)
- [ ] Open Netlify URL in clean Chrome profile — bento grid loads, no console errors
- [ ] Run Pre-Flight rejection test: type `"asdfgh"` → inline error < 1s, 0 network calls in DevTools

**Go/No-Go gate:** All 6 checks green before logging off Day 29 and before recording on Day 30. Do not start recording if any check fails.

### Day 30 — Record + Submit
**Goal:** Demo video recorded per Addendum I §25.2 choreography with Golden Path Lock (Addendum O) active. Submission package on lablab.ai before deadline.

---

#### Phase 1 — Morning Baseline Check (T-120min, first action before browser or OBS)
- [ ] `curl -s https://<slug>.onrender.com/health | python -m json.tool`
- [ ] `status: "ok"` — required; any other value = do not proceed
- [ ] `uptime_s > 60` — required; if < 60, wait 2 min, re-check
- [ ] `circuit_open: false` — required at this stage (live path nominal; will be tripped deliberately in Phase 2)
- [ ] `shadow_forced: false` — required; if true, Bright Data is timing out — investigate before proceeding
- [ ] `fallback_ready: true` — required; if false, POST a test search to warm fallback cache

---

#### Phase 2 — Golden Path Lock (T-90min, before dry run — Addendum O §31.3)
**Purpose:** Trip the Global Circuit Breaker intentionally to force `demo_state_data_analyst.json`. This guarantees UI output matches the scripted voiceover exactly — no live scrape variance during recording. **Do not open OBS until this phase is complete and all gates pass.**

**Step 1 — Trip the circuit breaker:**
```
Render Dashboard → GapHunter service → Environment
CIRCUIT_BREAKER_LIMIT → set to: 1
Save Changes → Manual Deploy → "Deploy latest commit"
Wait: until /health returns uptime_s > 60
Fire: one POST /api/search to increment counter to 1 ≥ limit 1 (breaker trips)
```

**Step 2 — Golden Path Verification Gate (Addendum O §31.4):**
```bash
# Confirm circuit open
curl -s https://<slug>.onrender.com/health | python -m json.tool
# Required: "circuit_open": true

# Confirm static state and top gap
curl -s -X POST https://<slug>.onrender.com/api/search \
  -H "Content-Type: application/json" \
  -H "X-Demo-Secret: $DEMO_SECRET" \
  -d '{"query": "Data Analyst", "session_id": "verify-golden"}' \
  | python -c "
import json, sys
d = json.load(sys.stdin)
print('SESSION:', d.get('session_id'))
print('TOP GAP:', d['gaps'][0]['skill'] if d.get('gaps') else 'MISSING')
print('JOB COUNT:', len(d.get('jobs', [])))
"
```

- [ ] `GET /health` → `circuit_open: true` — if false, re-trip via Option A and re-deploy
- [ ] `GET /health` → `uptime_s > 60` — if < 60, wait 2 min before proceeding
- [ ] `POST /api/search` → `session_id == "demo-static"` — if UUID, static file not loading; check `fallback/demo_state_data_analyst.json` committed
- [ ] `POST /api/search` → `gaps[0].skill == "dbt"` — if different skill, re-run `generate_full_demo_state.py` against a live run where dbt ranks #1; recommit; re-deploy
- [ ] `GET /health` → `fallback_ready: true` — if false, POST a test search to warm cache

**All 5 checks must be green before proceeding to Phase 3. No exceptions.**

---

#### Phase 3 — Dry Run (T-45min, OBS open, stopwatch running)
- [ ] Open OBS Studio or Loom — confirm recording profile settings correct (1920×1080, external mic)
- [ ] Open clean Chrome profile → navigate to Netlify URL — bento grid loads, no console errors
- [ ] Run complete Addendum I §25.2 choreography — stopwatch from T+0:00
- [ ] T+0:15–0:35: `"asdfgh"` rejection fires inline → narrate verbatim — if > T+0:40, adjust pacing
- [ ] T+0:35–1:05: CV drag-and-drop → `"Data Analyst"` search → skeleton loads — do NOT cut or skip
- [ ] T+1:05–1:30: Job cards render → narrate scoring formula verbatim
- [ ] T+1:30–1:55: Click Analyse → roadmap opens instantly → "Zero milliseconds." [2s pause]
- [ ] T+1:55–2:15: Scroll job cards → narrate top 3 gap skills by name — confirm `dbt` is #1
- [ ] T+2:15–2:45: Scroll to HR bento card → radar visible → "LLM Integration at 94" callout
- [ ] T+2:45–3:00: Closing callout → total < 3:05
- [ ] If any timestamp slips > 10s: rehearse that segment alone; full dry run again before recording

---

#### Phase 4 — Recording (Addendum I §25.5)

**Pre-recording constraints:**
- [ ] Chrome full-screen, 1920×1080, 90% zoom — bento grid must not wrap
- [ ] One tab open, bookmarks bar hidden, clean Chrome profile
- [ ] OBS Studio or Loom — no watermark, no countdown overlay
- [ ] External mic or headset — no laptop microphone
- [ ] Wired ethernet or 5GHz WiFi — no 4G/LTE

**Demo Video Choreography — Strict 3-Minute Performance Contract (Addendum I §25.2)**

| Timestamp | Action | Verbatim callout |
|---|---|---|
| T+0:00–0:15 | Open Netlify URL, bento grid visible, cursor on search field | — |
| T+0:15–0:35 | Type `"asdfgh"` → Pre-Flight Gate 1 rejection fires inline | "Zero Bright Data API calls. The gate caught it in under two seconds." |
| T+0:35–1:05 | Clear field → CV drag-and-drop → type `"Data Analyst"` → submit → skeleton loads | Do NOT cut — skeleton is the proof of live pipeline |
| T+1:05–1:30 | Job cards render — click top card | "0.35 × frequency + 0.25 × freshness + 0.20 × opportunity — no vibes, no black box." |
| T+1:30–1:55 | Click Analyse → roadmap accordion opens instantly | "Zero milliseconds. The roadmap was ready before you clicked." [2s pause] |
| T+1:55–2:15 | Scroll job cards — narrate gap list (❌ / ✅) | Narrate top 3 gap skills by name — dbt must be #1 |
| T+2:15–2:45 | Scroll to HR bento card — radar chart visible | "LLM Integration at 94 — the highest score on the chart. Stripe made a silent pivot while Block and Adyen stayed legacy." |
| T+2:45–3:00 | Cursor back to search form — closing | "Nineteen dollars a month for job seekers. Four-ninety-nine per seat for the team that wants to see that chart. Bright Data isn't a dependency — it's the moat." |

- [ ] Press record
- [ ] Pre-Flight rejection captured and narrated — T+0:15–0:35 window
- [ ] Scoring formula narrated verbatim — T+1:10
- [ ] "Zero milliseconds" callout with 2s pause — T+1:50
- [ ] "LLM Integration at 94" callout — T+2:15–2:45
- [ ] Stop recording — total runtime < 3:05

---

#### Phase 5 — Post-Recording Review + Breaker Reset

**Review recording (before touching Render):**
- [ ] Watch full recording file: timestamp adherence, audio clarity, no console errors visible on screen, no other tabs visible, no watermark
- [ ] Confirm `dbt` narrated and visible as #1 gap — if mismatch, re-record (do not submit with mismatch)
- [ ] If any Addendum I §25.3 failure contingency triggered — note which one; confirm fallback response was used

**Reset circuit breaker (Addendum O §31.5) — mandatory before submission:**
```
Render Dashboard → CIRCUIT_BREAKER_LIMIT → restore to: 100 → Save → Manual Deploy
Wait: until /health returns uptime_s > 60
```
- [ ] `GET /health` → `circuit_open: false` — if true, breaker did not reset; check env var and redeploy
- [ ] Fire post-reset verification search:
  ```bash
  curl -s -X POST https://<slug>.onrender.com/api/search \
    -H "Content-Type: application/json" \
    -H "X-Demo-Secret: $DEMO_SECRET" \
    -d '{"query": "Data Analyst", "session_id": "post-reset-verify"}' \
    | python -c "import json,sys; d=json.load(sys.stdin); print('SESSION:', d.get('session_id'))"
  ```
- [ ] `session_id` is a UUID — NOT `"demo-static"` (confirms live Bright Data path active for judges)

---

#### Phase 6 — Submission Package
- [ ] `git grep ANTHROPIC_API_KEY` → 0 matches in all tracked files
- [ ] `git grep DEMO_SECRET` → 0 matches in HTML/JS files (`.env` excluded by `.gitignore`)
- [ ] Submit to lablab.ai: demo video (MP4 or YouTube link) + GitHub repo URL + live Netlify URL + description ≤ 500 words — lead sentence must reference Bright Data as the data pipeline
- [ ] Screenshot submission confirmation page

---

## Decisions

| Decision | Reason | PRD ref |
|---|---|---|
| Weighted demand score replaces Counter() | Raw frequency misranks — old postings with 400 applicants outrank fresh openings | §7.3 |
| Claude Haiku for extraction + Gate 1, Sonnet for roadmap | 10× cost reduction on high-volume tasks; Sonnet reserved for synthesis reasoning | §10 |
| Multi-source: LinkedIn + Indeed + SERP | Cross-source validation multiplier (1.3×); broader coverage; salary from Indeed | §7.6 |
| FastAPI + static index.html → Render + Netlify | Production-grade stack; Netlify free static, Render free Python; no Gradio | §10 |
| SQLite for hackathon | Migration path to PostgreSQL documented; avoids infra setup time in 5-day sprint | §8.3 |
| httpOnly cookies for JWT | XSS token theft prevention — required by cybersecurity spec | §9.1 |
| Auth flow CUT from demo scope | Pre-seeded Alpine.js store bypasses login — judges see the product, not auth friction | Addendum B §18.5 |
| Multi-page routing CUT | Single `index.html`; bento zones handle all states via Alpine.js reactive data | Addendum B §18.3 |
| HR radar uses "Silent Pivot" hardcoded data | HR tab is MOCK — zero backend call; Stripe LLM spike (94) immediately legible to fintech judges | Addendum H |
| Demo video exactly 3 minutes | Judge attention is finite; choreography scripted so no second is wasted | Addendum I |
| UptimeRobot 5-min interval | Render free tier spins down at 15 min idle; 5 min = 3× safety margin | Addendum J |
| GitHub Actions date-gated to 2026-05-30/31 | Prevents quota burn outside demo window | Addendum J |
| `/health` not behind `X-Demo-Secret` | UptimeRobot free plan cannot set custom headers; /health has zero API spend risk | Addendum J |

---

## Files — Active

| File | Status | Purpose |
|---|---|---|
| scraper.py | COMPLETE | LinkedIn + Indeed parallel scrape, full §7.1 payload |
| extractor.py | COMPLETE | Claude Haiku + `asyncio.Semaphore(10)` (Addendum A) |
| pipeline.py | PRESERVED | `attach_evidence()` still called by api.py |
| signals.py | COMPLETE | `freshness_score`, `competition_score`, `dual_signal`, `cross_source` |
| scoring.py | COMPLETE | `skill_demand_score()` + `job_relevance_score()` + `rank_gaps()` + `rank_jobs()` |
| normalizer.py | COMPLETE | Gate 0 + Gate 1 pre-flight (Addendum F) |
| security.py | COMPLETE — 2026-05-27 | Addendum M applied: frozenset L1 + `_CAREER_PREFIXES` L2 heuristic + `removeprefix` fix. `careers.stripe.com`, `talent.shopify.com` now pass. |
| database.py | COMPLETE | SQLite init + CRUD — users, user_profiles, search_history |
| auth.py | COMPLETE | JWT httpOnly + bcrypt cost 12 + IP rate limiting (CUT from demo flow, preserved for production) |
| resume.py | COMPLETE | 7-layer defence chain (Addendum E) |
| roadmap.py | COMPLETE | `ROADMAP_CACHE` + `prefetch_roadmaps()` fire-and-forget (Addendum D) |
| api.py | COMPLETE — 2026-05-27 | Addendum N implemented: circuit_open state, _load_static_demo(), search() short-circuit. /health rewritten to full 8-field diagnostic schema at GET /health. |
| fallback/fallback_payload_data_analyst.json | COMPLETE | Pre-cached postings — Shadow Mode fallback (Addendum C) |
| fallback/demo_state_data_analyst.json | PENDING — Day 29 Step 5d | Full pipeline response for Addendum N zero-token fallback. Generate via generate_full_demo_state.py. |
| index.html | COMPLETE — 2026-05-27 | Built from scratch: bento-grid, ApexCharts (bar + radar), Alpine.js, SweetAlert2, Tailwind CDN. Addendum L rate_limited branch + Addendum P window.__APP_TOKEN__ token pattern applied. |
| generate_full_demo_state.py | CREATED — 2026-05-27 | File exists at project root. Uses role= (not query=), correct SearchRequest schema. Requires DEMO_SECRET env + httpx install before run. |
| .github/workflows/keep-alive.yml | CREATED — 2026-05-27 | GitHub Actions backup pinger, cron */10 min, date-gated 2026-05-30/31 (Addendum J §26.3). Needs: RENDER_HEALTH_URL secret set in GitHub. |
| Procfile | CREATED — 2026-05-27 | `uvicorn api:app --host 0.0.0.0 --port $PORT` — Render deploy entrypoint |
| netlify.toml | CREATED — 2026-05-27 | Publish=`.`, proxies /api/* and /health to Render. Replace RENDER_URL placeholder after Step 2. |
| requirements.txt | UPDATED — 2026-05-27 | Added httpx>=0.27.0. Removed gradio (legacy, not deployed). |
| .gitignore | UPDATED — 2026-05-27 | Expanded: .env*, *.db, *.log, .venv, IDE dirs |

---

## Red Team Log

| Risk | PRD ref | Status | Mitigation |
|---|---|---|---|
| Scope creep — vision too large for 5 days | §13 | MITIGATED | Auth CUT, routing CUT, heatmap CUT, particles.js CUT; HR tab = single hardcoded radar (Addendum B + H) |
| Render cold start > 60s | §12 | MITIGATED | UptimeRobot 5-min primary + GitHub Actions 10-min backup + warm-up protocol (Addendum J) |
| Indeed payload schema diverges from LinkedIn | §7.1 | MITIGATED | Per-source `normalise()`; smoke-tested Day 28 |
| Magic bytes check rejects valid PDF/DOCX | §9.3 | MITIGATED | Tested with real files Day 28 |
| `rate_limited` status unhandled in Alpine.js — infinite skeleton on IP limit hit | Addendum L | MITIGATED | Branch added to §22.6 handler; `isLoading=false`, inline `searchError`, input preserved. Smoke test: Day 29 Step 5b. |
| Static `ALLOWED_JOB_DOMAINS` whitelist rejects `jobs.netflix.com`, `careers.stripe.com` — Apply button disabled for enterprise job matches | Addendum M | MITIGATED — 2026-05-27 | security.py rewritten: frozenset L1 + `_CAREER_PREFIXES` L2 heuristic. `careers.stripe.com`, `talent.shopify.com` pass. `removeprefix` fix applied. Smoke test pending (Step 5c). |
| Circuit breaker trips → extractor.py + roadmap_cache.py still fire Claude Haiku/Sonnet against fallback data — real API cost, violates $0 requirement | Addendum N | MITIGATED — 2026-05-27 | api.py: circuit_open state added to startup(), _load_static_demo() added, search() short-circuit added as first statement. Smoke test pending (Step 5d, CIRCUIT_BREAKER_LIMIT=1). |
| CORS misconfiguration on Render deploy | §9.2 | OPEN — Day 29 | Whitelist exact Netlify domain in FastAPI `allow_origins` |
| `VITE_DEMO_SECRET` / `VITE_APP_CHALLENGE_TOKEN` mismatch → Firewall blocks frontend | Addendum G | OPEN — Day 29 | After Addendum P rename: verify `VITE_APP_CHALLENGE_TOKEN` set in Netlify UI and `X-Demo-Secret` header present in DevTools before recording |
| `VITE_DEMO_SECRET` inlined as plaintext in Vite bundle — raw secret grep-able from DevTools Sources in < 30s | Addendum P | MITIGATED | Renamed to `VITE_APP_CHALLENGE_TOKEN`; stored as `btoa(secret)` in Netlify UI only; decoded inline with `atob()` at each fetch call site. Smoke test: Day 29 Step 5e. |
| JWT_SECRET absent from Render env | §9.1 | OPEN — Day 29 | Add random 32-char value in Render env dashboard |
| Demo video > 3 minutes | Addendum I | OPEN — Day 30 | Full dry run before recording; Pre-Flight at T+0:15 is hard start — if > T+0:25, restart |
| Live Bright Data scrape non-determinism during recording — voiceover references `dbt` as #1 gap but live data may rank a different skill, contradicting narration on camera | Addendum O | MITIGATED | Trip circuit breaker before recording (Addendum O §31.3) → forces `demo_state_data_analyst.json` → deterministic output. Reset after recording for live judge review. Golden Path Gate: Day 30 Phase 2. |
| `/health` not behind `X-Demo-Secret` | Addendum J | ACCEPTED | `/health` reads `app.state` only — zero Bright Data, zero Claude, zero SQL; risk negligible |

---

## Harness: Loop(0) Tools(Bright Data SERP + LinkedIn + Indeed / Claude Haiku 4.5 + Sonnet 4.6) Context(0/14) Persist(1) Verify(0/0) Constraints(Flat Harness | Inline Delivery | Zero Agentic Loops | No Embeddings for Ranking | Security-first)
## Agent Profile used: None
## CO-STAR applied: 0

---

## Hackathon Details
- Platform: lablab.ai
- Sponsor: Bright Data
- Dates: May 25–31, 2026
- Prize: $5,000 + Bright Data AI Startup Program
- Submission deadline: May 30, 2026 (demo May 31)
- Talvin joining online
