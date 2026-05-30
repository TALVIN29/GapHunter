# GapHunter — Real-Time Labor Market Intelligence

> **Hackathon submission — Bright Data × lablab.ai · May 2026**  
> Built in 5 days. Dual-audience. Production-grade.

**Live demo:** `https://gaphunterdemo.netlify.app`  
**API health:** `https://gaphunter-api.onrender.com/health`

---

## What It Does

GapHunter is a dual-audience labor market intelligence platform. It answers two questions from live web data — no cached databases, no static CSVs.

| Audience | Question | Answer |
|---|---|---|
| **Job Seeker** | Which skills are blocking me from roles I can win today? | Weighted gap score ranked by frequency × freshness × competition |
| **HR / Enterprise** | What are competitors secretly building toward in their hiring? | Auto-detected competitor → live postings → Skill Priority Map + Talent Hunter |

---

## Architecture

```
Browser (Alpine.js — Netlify CDN)
        │  HTTPS
        ▼
FastAPI Backend (Render)
        │
        ├── Bright Data SERP API         ──→  Job URL discovery, market news, course links
        ├── Bright Data LinkedIn Dataset  ──→  Structured job posting extraction
        ├── Bright Data Web Unlocker      ──→  Company profile intelligence
        ├── Bright Data Scraping Browser  ──→  JS-heavy regional job boards
        │
        ├── Claude Haiku 4.5              ──→  CV skill extraction, role validation,
        │                                      competitor auto-detection, role typo correction
        │
        ├── Claude Sonnet 4.6             ──→  Gap synthesis, competitive intelligence bullets,
        │                                      Skill Priority Map, Talent Hunter personas,
        │                                      Training Roadmap generation
        │
        └── Financial Firewall            ──→  Demo token + circuit breaker + shadow mode
```

---

## Bright Data Integration (4 Products)

| Product | Usage |
|---|---|
| **SERP API** | Job URL discovery at query time, market news per company, course links per skill gap |
| **LinkedIn Dataset API** | Structured job posting extraction — title, company, skills, salary, applicants |
| **Web Unlocker** | Company profile pages (Glassdoor/Indeed) for competitor intelligence |
| **Scraping Browser** | JS-heavy regional job boards inaccessible to standard scrapers |

All data fetched **at query time** — not pre-indexed, not cached from a previous scrape.

---

## Features

### Job Seeker Track

**F1 — Resume Upload (7-Layer Defence)**  
Upload PDF or DOCX. Claude Haiku extracts skills, experience years, seniority. File bytes discarded immediately after extraction — nothing stored.

Defence layers: size cap (5 MB) → magic byte detection → zip bomb guard (15 MB uncompressed) → character truncation (10,000 chars) → injection fence → Haiku model isolation → JSON output validation.

**F2 — Real-Time Job Search**  
Role + location → Bright Data SERP → URL discovery → parallel LinkedIn Dataset extraction → Claude Haiku skill extraction → weighted gap ranking → job card rendering.

Scoring formula: `0.35 × frequency + 0.25 × freshness + 0.20 × opportunity + cross-platform bonuses`

**F3 — Weighted Gap Analysis**  
ApexCharts horizontal bar. Skills ranked by composite demand signal, not raw keyword frequency. Dual-source bonuses reward skills appearing across LinkedIn AND Indeed simultaneously.

**F4 — Optimistic Roadmap Pre-fetching**  
Roadmaps fire as `asyncio.create_task()` the moment job cards render. Bright Data SERP finds live Coursera/YouTube resources per skill. Perceived wait time: 0ms.

### Enterprise Track

**F5 — Competitor Auto-Detection**  
Enter your company name. Claude detects the primary competitor in your market automatically — no manual input required. Returns competitor name + detection rationale + recent market news.

**F6 — Live Competitor Hiring Intelligence**  
Bright Data scrapes up to 5 live competitor postings. Parallel pipeline fires three AI analysis threads:

1. **Competitive Intelligence** (~15s) — What competitor is building vs. what you're missing
2. **Skill Priority Map** (~30s) — Critical / Growing / Emerging tiles from live signal
3. **Talent Hunter** (~45s) — 3 candidate personas with leaving signals + LinkedIn search + outreach template

**F7 — Training Roadmap**  
Per priority skill: Bright Data SERP finds real course links at analysis time. Not hardcoded — every link is a live page discovered during the request.

---

## Financial Firewall

> **Important for judges testing the platform:**

| Layer | Mechanism | Limit |
|---|---|---|
| **Layer 1** | `X-Demo-Secret` header required | `403` without it |
| **Layer 2** | IP rate limiter (unauthenticated only) | 5 req/hr per IP |
| **Layer 3** | Circuit breaker — trips after N live searches | Serves static demo, $0 cost |

**To test without hitting the rate limit:**  
Add header `X-Demo-Secret: <your-demo-token>` to API requests. The Netlify frontend sends this automatically — rate limiting only affects raw API calls.

**If `circuit_open: true` in `/health`:** Daily budget exhausted. Platform serves pre-scraped static demo with zero additional cost. Demo remains fully functional.

---

## Security Hardening

| Threat | Defence |
|---|---|
| OOM JSON bomb | Pure ASGI `ContentLengthGuard` — 2 MB hard cap, stream-counted for chunked bodies |
| DOCX zip bomb | ZIP central-directory scan (zero decompression) before `python-docx` |
| Prompt injection via resume | System prompt injection fence + model isolation (Haiku, not Sonnet) |
| Session cache poisoning | `POISON_GUARD`: reserved `session_id="demo-static"` overridden with fresh UUID |
| Memory exhaustion | `_LRUSessionCache`: `OrderedDict`-backed, 300-session cap, O(1) eviction |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Alpine.js, Tailwind CSS, ApexCharts, GSAP, Netlify CDN |
| Backend | FastAPI, Python 3.12, Uvicorn, Render |
| AI | Anthropic Claude Haiku 4.5 (extraction/detection) + Claude Sonnet 4.6 (synthesis/intelligence) |
| Data | Bright Data SERP API + LinkedIn Dataset API + Web Unlocker + Scraping Browser |
| Auth | Demo secret (HMAC-safe compare) |
| Infra | Render (backend) + Netlify (frontend) |

---

## E2E Test Results

| Test | Scenario | Result |
|---|---|---|
| V1 | OOM bomb `Content-Length: 3 MB` | HTTP 413 |
| V2 | 8 authenticated requests | 0× rate_limited |
| V3 | `session_id="demo-static"` poison attempt | Fresh UUID returned |
| V4 | Scrape timeout fallback | Static demo in 4.85s |
| T4A | Circuit breaker trip (limit=1) | `circuit_open: true` |
| T4B | Zero-LLM circuit path | 403ms |
| T4C | Roadmap poll from startup cache | `status: ready` |

---

## Pricing Model

| Tier | Price | Audience |
|---|---|---|
| Job Seeker | $19 / month | Individual candidates |
| HR Enterprise | $499 / seat / month | Recruiters, HR teams |

---

## Repository Structure

```
api.py                  — FastAPI backend (all endpoints, firewall, circuit breaker)
index.html              — Alpine.js frontend (single file, both tracks)
scraper.py              — Bright Data SERP + LinkedIn Dataset integration
resume.py               — 7-layer resume pipeline
extractor.py            — Parallel Claude Haiku skill extraction
pipeline.py             — Gap scoring + evidence attachment
normalizer.py           — Role validation (Gate 0 + Gate 1)
roadmap.py              — Optimistic pre-fetching + LRU session cache
scoring.py              — Weighted demand scoring formula
signals.py              — Competitor intelligence signal processing
security.py             — ASGI content guards, injection fencing
auth.py                 — Demo token auth + HMAC comparison
database.py             — SQLite search history
fallback/               — Pre-scraped static demo state (circuit_open fallback)
PRD.md                  — Full product requirements (locked)
TEST_REPORT_FINAL.md    — Pre-submission E2E test report
DECISION_LOG.md         — Key architectural decisions log
```

---

*Built with Bright Data + Anthropic Claude · lablab.ai Hackathon · May 2026*
