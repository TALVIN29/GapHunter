# GapHunter — Hackathon Fix Plan
## Bright Data AI Agents Web Data Hackathon · lablab.ai
## Target: Maximum score across Track 1 (GTM Intelligence) + Track 2 (Finance & Market Intelligence)

---

## Progress Tracker

```
Status: IN_PROGRESS
Progress: 0/6 fixes complete = 0%
Last checkpoint: Plan written
Next action: Execute F1 → F6 in order
```

| ID | Fix | File(s) | Priority | Status |
|---|---|---|---|---|
| F1 | Replace fake demo company names | `fallback/demo_state_data_analyst.json` | HIGH | ⬜ PENDING |
| F2 | Add Bright Data Web Unlocker to `/api/analyse` | `api.py`, `scraper.py` | HIGH | ⬜ PENDING |
| F3 | Reframe UI as AI Agent | `index.html` | HIGH | ⬜ PENDING |
| F4 | Rewrite README for hackathon judges | `README.md` | HIGH | ⬜ PENDING |
| F5 | Pass demand score into roadmap generation | `roadmap.py`, `api.py` | MEDIUM | ⬜ PENDING |
| F6 | Commit, push, verify deploy | — | HIGH | ⬜ PENDING |

---

## Hackathon Requirement Audit

### Mandatory: Bright Data Tool Integration

| Bright Data Tool | Currently Used? | Where |
|---|---|---|
| SERP API | ✅ YES | Job URL discovery (scraper.py), roadmap resource fetch (roadmap.py) |
| Web Scraper API / Jobs Dataset | ✅ YES | Structured job extraction (scraper.py) |
| Web Unlocker | ❌ NO | **F2: Add to `/api/analyse` for real job page content** |
| MCP Server | ❌ NO | Out of scope — SERP + Web Scraper + Unlocker covers 3 tools |
| Scraping Browser | ❌ NO | Out of scope for now |

**Verdict:** Currently 2 Bright Data tools. After F2: 3 tools (SERP, Web Scraper, Web Unlocker).

---

### Track Alignment Audit

**Track 1 — GTM Intelligence (Competitive hiring signals)**

| Criterion | GapHunter Feature | Status |
|---|---|---|
| Continuously monitor competitors | HR Competitor Intel tab — `/api/hr/competitors` | ✅ LIVE |
| Surface intelligence into GTM workflows | Skill demand radar + ranked gap chart | ✅ LIVE |
| Hiring signals tracking | Demand score = frequency × freshness × competition | ✅ LIVE |
| AI agents acting autonomously | 5-step pipeline: validate → scrape → extract → synthesize → prefetch | ✅ EXISTS — needs UI framing |

**Track 2 — Finance & Market Intelligence**

| Criterion | GapHunter Feature | Status |
|---|---|---|
| Alternative data pipelines — job postings | LinkedIn + Indeed parallel scraping | ✅ LIVE |
| Multi-source synthesis engines | Dual-source bonus + cross-source confirmation | ✅ LIVE |
| Structured intelligence objects | Gap object: `{skill, demand_score, urls, why_it_matters}` | ✅ LIVE |
| Salary / pricing trend data | Salary range extracted from every posting | ✅ LIVE |

---

## Fix Specifications

### F1 — Replace Fake Demo Company Names

**Problem:** `fallback/demo_state_data_analyst.json` has placeholder names: GrowthCo, DataDriven Startup, FinanceFlow Ltd., TechCorp Inc., RetailGiant Corp. These are clearly fake and reduce credibility.

**Fix:** Replace with real publicly known companies hiring Data Analysts in 2026.

**Companies to use:** Airbnb, Stripe, DoorDash, Lyft, Databricks

**Files:** `fallback/demo_state_data_analyst.json`

**Acceptance:** All 5 job cards show real company names. Apply URLs still work.

---

### F2 — Bright Data Web Unlocker for Real Job Page Content

**Problem:** `/api/analyse` currently passes only job metadata (title, company, salary) to Claude. Claude cannot access LinkedIn/Indeed pages directly. The analysis is good but not grounded in the actual job posting text.

**Fix:** When `job_url` is provided, use Bright Data Web Unlocker to fetch the actual job page HTML, extract the job description text, and pass it to Claude. Fall back to metadata-only if Unlocker fails or returns empty.

**Architecture:**
```
POST /api/analyse
  → validate URL
  → Bright Data Web Unlocker fetch(job_url)   ← NEW: real job page content
  → extract job description text from HTML
  → build Claude prompt with real content + metadata
  → Claude Sonnet analysis
  → return {highlight_skills, gap_skills, application_tip}
```

**Bright Data endpoint:** `https://api.brightdata.com/request` (Web Unlocker)

**Files:** `api.py` (analyse endpoint), optionally `scraper.py` (add `fetch_with_unlocker()`)

**Acceptance:** Render logs show `Web Unlocker` call on `/api/analyse`. Claude response references actual job requirements, not generic role advice.

**Fallback:** If Unlocker times out or returns < 200 chars, silently fall back to metadata-only prompt. Never fail the endpoint.

---

### F3 — Reframe UI as AI Agent

**Problem:** The UI says "GapHunter" with a generic subtitle. It looks like a search form. The hackathon is an "AI Agents" hackathon — judges need to immediately understand this is an autonomous AI agent pipeline.

**Fix:**
1. Update header: "GapHunter — AI Labor Intelligence Agent"
2. Add subtitle: "Autonomous AI agent that scrapes the live web, extracts signals, and surfaces the exact skills blocking you from roles you can win."
3. Add "Agent Pipeline" info strip below the search panel showing the 5 steps: Validate → Scrape → Extract → Synthesize → Pre-fetch
4. Rename "Find Gaps" button to "Run Agent"

**Files:** `index.html`

**Acceptance:** First visible screen clearly communicates "AI Agent" — no ambiguity.

---

### F4 — Rewrite README for Hackathon Judges

**Problem:** Current README is good technically but doesn't lead with the "AI Agent" framing, doesn't clearly show the Bright Data tools used, and doesn't explicitly call out the hackathon track alignment.

**Fix:**
1. Lead with: "GapHunter is an autonomous AI agent…"
2. Add section: "Bright Data Integration — 3 Tools"
3. Add section: "Hackathon Track Alignment"
4. Keep: Financial Firewall warning for judges
5. Add: Agent pipeline diagram (ASCII)

**Files:** `README.md`

---

### F5 — Demand Score Context in Roadmap Generation

**Problem:** `_generate_roadmap()` in `roadmap.py` generates `why_it_matters` but has no context about HOW demanded the skill is or WHY it ranked highly. The explanation is generic ("Python is popular").

**Fix:** Pass the skill's demand score and ranking context into the `_USER_TEMPLATE`. Claude then explains the market signal — e.g., "dbt ranked #1 with demand score 0.82, appearing in 14/17 postings, concentrated in freshest low-competition roles — meaning the market is actively hiring for this skill right now."

**Files:** `roadmap.py` (update `_USER_TEMPLATE` and `_prefetch_one`), `api.py` (pass score to `prefetch_roadmaps`)

---

### F6 — Commit, Push, Verify Deploy

**Checklist:**
- [ ] All 5 fixes applied
- [ ] `git diff` reviewed — no API keys, no `.env` content, no secrets in diff
- [ ] `git add` specific files only (no `git add -A`)
- [ ] Push to origin/main
- [ ] Render deploy detected (uptime < 120s)
- [ ] `/health` returns: `status=ok, circuit_open=false, shadow_forced=false, fallback_ready=true`
- [ ] Manual smoke test: run one search, check job cards, check analyse, check roadmap

---

## Security Audit (Per User Requirement)

Every file touched must be checked for leaked secrets before commit.

| Risk | Check | Files |
|---|---|---|
| API keys in source | `grep -r "ANTHROPIC\|BRIGHTDATA\|sk-ant\|Bearer " --include="*.py" --include="*.html" --include="*.json"` | All |
| `.env` committed | `git status` — `.env` must NOT appear | — |
| Hardcoded demo secret | `DEMO_SECRET` must only appear as env var reference, never value | `api.py` |
| LinkedIn session cookies | Must not appear anywhere | `scraper.py` |

**`.gitignore` must include:** `.env`, `*.env`, `__pycache__/`, `*.pyc`

---

## Definition of Done

Platform is submission-ready when:

1. All 6 fixes marked ✅ COMPLETE
2. `/health` GREEN on production
3. Job apply links open real search results
4. `/api/analyse` logs show Bright Data Web Unlocker call
5. UI header reads "AI Labor Intelligence Agent"
6. README leads with "autonomous AI agent"
7. No secrets in git history (verified with grep)
8. 3-minute demo can be recorded without a broken step
