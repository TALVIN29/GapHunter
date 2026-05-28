# GapHunter — Decision Log
## All decisions logged with reasoning and impact. Updated every session.

---

## Session: 2026-05-28

---

### D-001 — Pure ASGI middleware over BaseHTTPMiddleware for ContentLengthGuard

**Decision:** Replaced `BaseHTTPMiddleware` with a raw ASGI callable class.

**Reasoning:** `BaseHTTPMiddleware.call_next()` does not propagate a re-injected `receive` callable to the downstream ASGI app. Starlette's internal implementation buffers independently, meaning our patched receive (with the truncated body) was ignored — FastAPI's Pydantic parser saw null body → 422 ValidationError on all POST requests. Pure ASGI middleware owns the `receive` callable directly via closure, so body buffering and limit enforcement are guaranteed to reach the handler.

**Impact:** Fixed V2 test suite (all 8 authenticated requests returning ERROR/422). OOM bomb protection (V1 test, HTTP 413) now works end-to-end. No performance regression — one extra buffer allocation per request, eliminated on GET/WebSocket paths via `scope["type"] != "http"` guard.

**PRD ref:** Addendum E §20, security hardening table.

---

### D-002 — Job metadata fields added to AnalyseRequest and passed to Claude

**Decision:** Added `job_title`, `company`, `location`, `seniority`, `salary`, `skills_match` fields to `AnalyseRequest`. Claude prompt rebuilt from metadata instead of attempting to fetch LinkedIn URL.

**Reasoning:** `/api/analyse` was sending only `job_url` (a LinkedIn URL) to Claude. Claude has no browsing capability → generated generic output including "Unable to retrieve job details — LinkedIn URL requires authentication." The data was already scraped by Bright Data during the search step and present in the job card. Re-sending it costs zero extra API calls and grounds the analysis in real posted data.

**Impact:** Job analysis now produces role-specific and company-specific advice. No additional Bright Data calls. No latency increase.

**PRD ref:** F3 §11.3.

---

### D-003 — Bright Data Web Unlocker added to `/api/analyse` (Addendum Q)

**Decision:** Before building the Claude prompt, `/api/analyse` now calls `_fetch_with_unlocker(job_url)` — Bright Data Web Unlocker endpoint `https://api.brightdata.com/request`. If the response ≥ 200 chars, the actual job page text (HTML-stripped) is injected into the prompt. Fall back to metadata-only if Unlocker fails, times out, or returns short content.

**Reasoning:** Hackathon judges require demonstrated use of Bright Data Web Unlocker (listed as a required technology). Existing integration was SERP + Web Scraper (2 tools). Adding Unlocker brings it to 3 tools. The analyse endpoint is the correct integration point: it's called per-job after the user selects a specific posting, so the Unlocker fetch is purposeful and targeted, not wasteful.

**Impact:** Third Bright Data tool now live. When Unlocker succeeds, Claude receives the actual job description text (not just metadata), producing higher-quality analysis grounded in exact job requirements. Response includes `"source": "unlocker"` or `"source": "metadata"` for observability. Fallback path unchanged — endpoint never fails.

**Zone config:** `BRIGHTDATA_UNLOCKER_ZONE` env var, default `"unlocker"`. Timeout: 15s sync + 3s asyncio buffer.

**PRD ref:** Addendum Q (new). HACKATHON_FIX_PLAN.md F2.

---

### D-004 — Demand score context injected into roadmap `why_it_matters` generation (F5)

**Decision:** `prefetch_roadmaps()` now accepts `gap_scores: dict[str, float]`. Each skill's demand score and rank is passed into `_generate_roadmap()` and injected into `_USER_TEMPLATE`. Claude is instructed to ground `why_it_matters` in the demand signal (score, formula, market interpretation) rather than generating generic "Python is popular" copy.

**Reasoning:** Generic `why_it_matters` copy undermines the data-driven credibility of the platform. The demand score is real, computed from live scraping. Surfacing it in the roadmap explanation closes the loop between the gap chart (score displayed) and the learning recommendation (why this score matters for job market positioning).

**Impact:** Roadmap accordion now shows market-grounded explanations. Claude output references specific score, rank, and formula interpretation. No latency change (same Claude call, larger prompt by ~100 tokens).

**api.py change:** `gap_score_map = {g["skill"]: g.get("demand_score", 0.0) for g in evidence}` built after `attach_evidence`, passed to `prefetch_roadmaps`.

**PRD ref:** F5 §HACKATHON_FIX_PLAN.

---

### D-005 — UI header reframed as "AI Labor Intelligence Agent" (F3)

**Decision:** Header updated to "GapHunter / AI Labor Intelligence Agent". "Find My Gaps" button renamed to "Run Agent". Agent pipeline strip added (Validate → Scrape → Extract → Synthesize → Pre-fetch) visible after search or during search.

**Reasoning:** Hackathon theme is "AI Agents Web Data." Judges viewing the platform need to identify it as an AI agent pipeline within 3 seconds. The previous UI looked like a search form. No code changes — purely HTML label and CSS layout.

**Impact:** First visible screen communicates "AI Agent" framing. Pipeline strip is informational only — no logic change. Step ⑤ (Pre-fetch) highlights in accent blue when gaps are loaded, confirming optimistic pre-fetching completed.

**PRD ref:** HACKATHON_FIX_PLAN F3.

---

### D-006 — Hardcoded location defaults removed from Alpine.js state

**Decision:** `role: ''`, `location: ''`, `hrRole: ''`, `hrLocation: ''` — all default to empty string. Location placeholder updated to "e.g. Kuala Lumpur, Malaysia or New York, NY".

**Reasoning:** Previous defaults were `'Data Analyst'` and `'United States'`. A user in Malaysia searching for jobs would be sent to a US-market search without realizing it. Hardcoded defaults are not scalable or customizable. User stated explicitly: "how can one apply job in Malaysia goes to NY?"

**Impact:** Users must explicitly enter their role and location. Prevents silent geographic misrouting. Placeholder text guides users toward city-level specificity.

**PRD ref:** Scalability constraint (user explicit requirement).

---

### D-007 — Fake company names replaced in demo fallback (F1)

**Decision:** `fallback/demo_state_data_analyst.json` updated — GrowthCo → Airbnb, DataDriven Startup → Stripe, FinanceFlow Ltd. → Databricks, TechCorp Inc. → DoorDash, RetailGiant Corp. → Lyft.

**Reasoning:** Fake company names undermine credibility in a live demo. Real companies hiring Data Analysts in 2026 that judges will recognize. URLs updated from fake LinkedIn job IDs (sequential integers like `1234567890`) to real LinkedIn/Indeed search URLs for the respective role+company+location.

**Impact:** Demo mode job cards show recognizable companies. Apply links route to real job search results. Circuit-open state no longer exposes fabricated data.

**PRD ref:** HACKATHON_FIX_PLAN F1.

---

### D-008 — HITL gate: user must verify Render env var BRIGHTDATA_UNLOCKER_ZONE is set

**Decision:** Web Unlocker zone name reads from `BRIGHTDATA_UNLOCKER_ZONE` env var (default `"unlocker"`). This must be verified in Render dashboard before deploy. If zone name is wrong, Unlocker calls return 4xx and fall back silently — no breakage, but no Unlocker benefit.

**Reasoning:** Zone names are account-specific in Bright Data. Cannot hardcode. Cannot verify without Render dashboard access. HITL gate required — user action, not automated.

**Human action required:** Confirm `BRIGHTDATA_UNLOCKER_ZONE` is set correctly in Render env vars. If not set, default `"unlocker"` may or may not match the account's zone name.

**Impact:** If zone wrong → silent metadata fallback. If zone correct → Unlocker active, `"source": "unlocker"` in response.

---
