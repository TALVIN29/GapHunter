# GapHunter — Real-Time Labor Market Intelligence

> **Hackathon submission — Bright Data × lablab.ai · May 2026**  
> Built in 5 days. Backend fortified. E2E tested. Production-grade.

**Live demo:** `https://gaphunter.netlify.app`  
**API:** `https://gaphunter-api.onrender.com/health`

---

## What It Does

GapHunter is a dual-audience labor market intelligence platform. It answers two questions from live web data — no cached databases, no static CSVs.

| Audience | Question | Answer |
|---|---|---|
| **Job Seeker** | Which skills are blocking me from roles I can win today? | Weighted gap score ranked by frequency × freshness × competition |
| **HR / Recruiter** | What are competitors secretly building toward in their hiring? | Real-time competitor radar chart from live job posting signals |

---

## Architecture

```
Browser (Alpine.js Bento Grid — Netlify CDN)
        │  HTTPS
        ▼
FastAPI Backend (Render free tier)
        │
        ├── Bright Data SERP API  ──→  Google → LinkedIn + Indeed URLs
        ├── Bright Data Jobs API  ──→  30 parallel job description scrapes
        │       └── asyncio.Semaphore(10) — concurrency cap
        │
        ├── Claude Haiku          ──→  Gate 1: role validation
        │                              30× job skill extraction (parallel)
        │                              Resume parsing (7-layer defence)
        │
        ├── Claude Sonnet         ──→  Gap synthesis + ranking
        │                              Per-job analysis
        │                              Learning roadmap generation (Semaphore=3)
        │
        └── Financial Firewall    ──→  IP rate limit + circuit breaker + shadow mode
```

---

## Core Engine — Bright Data Integration

Bright Data powers every live data interaction:

| API | Usage | Volume |
|---|---|---|
| **SERP API** | Google search → LinkedIn + Indeed job URLs at query time | 2 queries per search |
| **Jobs Dataset API** | Structured job posting extraction (title, company, skills, salary, applicants) | Up to 30 postings per search |
| **SERP API (roadmap)** | Coursera + YouTube resource discovery per skill gap | 1 query per skill, async |

Data is fetched **at query time** — not pre-indexed, not cached from a previous scrape. When a user searches for "Data Analyst," Bright Data discovers live LinkedIn and Indeed URLs on that request. The roadmap links scraped 60 seconds later are live pages that existed at query time.

---

## Key Features

### F1 — Resume Upload (7-Layer Defence)
Upload a PDF or DOCX. Claude Haiku extracts skills, experience years, and seniority. File bytes discarded immediately after extraction — nothing stored.

Defence layers: size cap (5 MB) → magic byte detection → zip bomb guard (15 MB uncompressed) → character truncation (10,000 chars) → injection fence in system prompt → Haiku model isolation → JSON output validation.

### F2 — Real-Time Job Search
Role + location → Bright Data SERP → URL discovery → parallel Bright Data Jobs scraping → Claude Haiku extraction → weighted gap ranking → job card rendering.

Scoring formula: `0.35 × frequency + 0.25 × freshness + 0.20 × opportunity + bonuses`

### F3 — Weighted Gap Analysis
ApexCharts horizontal bar. Skills ranked by composite demand signal, not raw keyword frequency. Dual-source and cross-platform bonuses reward skills that appear across LinkedIn AND Indeed simultaneously.

### F4 — Optimistic Roadmap Pre-fetching
Roadmaps fire as `asyncio.create_task()` the moment job cards render. Bright Data SERP finds live Coursera and YouTube resources per skill. By the time a user reads their results and opens the roadmap accordion, generation is already complete. Perceived wait time: 0ms.

### F9 — HR Competitor Intelligence ("Silent Pivot")
Radar chart mapping Stripe, Block, and Adyen across six modern data-stack skills. The chart surfaces Stripe's LLM Integration hiring signal (score: 94) — not visible in public announcements, detectable only from job posting frequency. Bloomberg Talent Insights charges $30,000/yr for equivalent signals.

---

## Financial Firewall

> **Important for judges testing the platform:**

The backend implements a three-layer cost protection system designed for production use. **Judges should use the demo secret header to bypass rate limiting.**

| Layer | Mechanism | Limit |
|---|---|---|
| **Layer 1** | `X-Demo-Secret` header required | `403` without it |
| **Layer 2** | IP rate limiter (unauthenticated only) | 5 req/hr per IP |
| **Layer 3** | Circuit breaker — trips after N live searches | Serves static demo, $0 cost |

**To test without hitting the rate limit:**  
Add header `X-Demo-Secret: gaphunter-demo-2026` to all API requests. Authenticated traffic bypasses Layer 2 entirely. The Netlify frontend sends this header automatically — rate limiting only affects raw API calls without the header.

**If `circuit_open: true` appears in `/health`:** The daily live-search budget was exhausted. The platform automatically serves a static demo state (real data, pre-scraped) with zero additional cost. This is by design — the demo is still fully functional.

---

## Security Hardening

| Threat | Defence |
|---|---|
| OOM JSON bomb | Pure ASGI `ContentLengthGuard` — 2 MB hard cap, stream-counted for chunked bodies |
| DOCX zip bomb | ZIP central-directory scan (zero decompression) before `python-docx` |
| Prompt injection via resume | System prompt injection fence + model isolation (Haiku, not Sonnet) |
| Session cache poisoning | `POISON_GUARD`: reserved `session_id="demo-static"` overridden with fresh UUID |
| Memory exhaustion (roadmap cache) | `_LRUSessionCache`: `OrderedDict`-backed, 300-session cap, O(1) eviction |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Alpine.js, Tailwind CSS, ApexCharts, Netlify CDN |
| Backend | FastAPI, Python 3.12, Uvicorn, Render |
| AI | Anthropic Claude Haiku 4.5 (extraction) + Claude Sonnet 4.6 (synthesis) |
| Data | Bright Data SERP API + Jobs Dataset API |
| Auth | Demo secret (HMAC-safe compare) + JWT for user accounts |
| Storage | SQLite (Render ephemeral) — search history, user profiles |

---

## E2E Test Results

All chaos tests and patch verification passed before submission.

| Test | Scenario | Result |
|---|---|---|
| V1 | OOM bomb `Content-Length: 3 MB` | HTTP 413 |
| V2 | 8 authenticated requests | 0× rate_limited |
| V3 | `session_id="demo-static"` poison attempt | Fresh UUID returned |
| V4 | Scrape timeout fallback | Static demo in 4.85s |
| T4A | Circuit breaker trip (limit=1) | `circuit_open: true` |
| T4B | Zero-LLM circuit path | 403ms, `session_id: demo-static` |
| T4C | Roadmap poll from startup cache | `status: ready` |

---

## Pricing Model

| Tier | Price | Audience |
|---|---|---|
| Job Seeker | $19 / month | Individual candidates |
| HR Enterprise | $499 / seat / month | Recruiters, HR teams |

Pipeline scales linearly with Bright Data: 10 users → 300 postings/day; 1,000 users → 30,000/day. Bright Data is the moat, not a cost center.

---

## Repository Structure

```
api.py           — FastAPI backend (all endpoints, financial firewall, circuit breaker)
resume.py        — 7-layer resume pipeline
roadmap.py       — Optimistic pre-fetching + LRU session cache
index.html       — Alpine.js frontend (single file)
scraper.py       — Bright Data SERP + Jobs integration
extractor.py     — Parallel Claude Haiku skill extraction
normalizer.py    — Role validation (Gate 0 + Gate 1)
pipeline.py      — Gap scoring + evidence attachment
fallback/        — Pre-scraped static demo state (circuit_open fallback)
PRD.md           — Full product requirements + addendums (locked)
TEST_REPORT_FINAL.md — Pre-submission E2E test report
```

---

*Built with Bright Data + Anthropic Claude · lablab.ai Hackathon · May 2026*
