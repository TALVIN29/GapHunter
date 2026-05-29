# GapHunter E2E Test Plan — v2.0
**Date:** 2026-05-29 | **Deadline:** 2026-05-31 | **Environment:** Production (Netlify + Render)

---

## Pre-Test Setup

| Step | Action | Expected |
|------|--------|---------|
| P1 | Open your Netlify URL | Page loads, header shows "Labor Market Intelligence · Powered by Bright Data" |
| P2 | Check header strip | Both lines visible: "For individuals" and "For enterprises" |
| P3 | Check top-right badge | Shows **Live** (green), not Demo Mode |
| P4 | Open Render logs tab (keep open) | Ready to capture logs during tests |

---

## Module 1 — Input Validation

| ID | Test | Steps | Expected | Pass/Fail |
|----|------|-------|---------|-----------|
| V1 | Empty submit | Click "Run Agent" with nothing filled | Red border + shake on role field, no popup | |
| V2 | Role clears error | Type anything in role field after V1 | Red border disappears | |
| V3 | Empty role, has location | Fill location only → Run Agent | Red border on role field | |
| V4 | Invalid role (garbage) | Type "asdfjkl123" → Run Agent | Either inline error or "Invalid Query" toast | |

---

## Module 2 — CV Upload

| ID | Test | Steps | Expected | Pass/Fail |
|----|------|-------|---------|-----------|
| C1 | Upload PDF | Click upload area → select a real PDF resume | "Parsing…" → "✓ X skills extracted · Role detected: Y" | |
| C2 | Role auto-fills | After C1 | Role field shows inferred role, green border, "Auto-detected from CV" badge | |
| C3 | Run Agent after CV only | After C2, no location, click Run Agent | Search runs without location, returns global jobs | |
| C4 | Upload DOCX | Upload a `.docx` resume | Same as C1 | |
| C5 | Upload wrong file type | Try uploading a `.txt` or `.jpg` | Graceful error message, no crash | |

---

## Module 3 — Job Search (Individual Intelligence)

| ID | Test | Steps | Expected | Pass/Fail |
|----|------|-------|---------|-----------|
| S1 | Basic search | "Data Analyst" + "Kuala Lumpur" → Run Agent | Progress steps advance showing Bright Data tools, jobs return in ~50s | |
| S2 | Progress steps | Watch during S1 | 6 steps visible, naming "Bright Data SERP", "LinkedIn Dataset" etc. | |
| S3 | Bright Data badges | After S1 results load | Badge strip shows: Bright Data SERP · LinkedIn Dataset · Claude AI | |
| S4 | Job count display | After S1 | "X jobs from Y postings analysed in Kuala Lumpur" visible | |
| S5 | Location "KL" short form | "Data Analyst" + "KL" → Run Agent | Returns Malaysia jobs, NOT California or US results | |
| S6 | Singapore search | "Software Engineer" + "Singapore" → Run Agent | Returns SG jobs | |
| S7 | No location | "Product Manager" + no location → Run Agent | Returns jobs (global), no crash | |
| S8 | Title relevance filter | Check job titles in results | All titles related to searched role (no "Sales Manager" for "Data Analyst") | |
| S9 | Source badge | Each job card | Shows "linkedin", "wobb", or other real source | |
| S10 | Verified badge | Each job card | "✓ Verified" visible on all cards | |
| S11 | Apply link | Click "Apply →" on any job | Opens real URL in new tab, not a fake or broken link | |
| S12 | Date format | Check posted date on job detail | Shows "3 days ago" / "1 week ago", NOT raw ISO timestamp | |

---

## Module 4 — Gap Analysis Tab

| ID | Test | Steps | Expected | Pass/Fail |
|----|------|-------|---------|-----------|
| G1 | Default tab | After S1, click any job | Gap Analysis tab active by default | |
| G2 | Demand score formula card | Gap Analysis tab visible | Formula card shows: "score = (0.35 × frequency + 0.25 × freshness + ...)" with coloured signals | |
| G3 | Highlight skills | Gap Analysis → Highlight section | Shows 5 skills to emphasise | |
| G4 | Gap skills | Gap Analysis → Skills to Develop | Shows 3 skill gaps | |
| G5 | Application tip | Gap Analysis → Application Tip | Shows company-specific actionable tip, not generic | |
| G6 | Gap chart renders | Scroll to gap chart section | Bar chart renders with 5 skill bars and demand scores | |
| G7 | Roadmap accordion | Click a skill in roadmap section | Expands with steps, why_it_matters, estimated total time | |
| G8 | Formula in roadmap | Open any roadmap | Shows demand score + full formula reference at bottom | |
| G9 | Job change resets | Click different job card | Gap analysis clears and reloads for new job | |

---

## Module 5 — Company Tab

| ID | Test | Steps | Expected | Pass/Fail |
|----|------|-------|---------|-----------|
| CP1 | Known company loads | Click Company tab on EPAM, Maybank, Meltwater | Rating, pros, cons, culture summary visible | |
| CP2 | Data source shown | Check sources at bottom | Shows "glassdoor", "indeed", or "google" in sources_used | |
| CP3 | Unknown company | Click Company tab on small/unknown company | Graceful fallback — "Insufficient data" or Claude knowledge, no crash | |
| CP4 | Bright Data attribution | Bottom of company tab | "via Bright Data" label visible | |

---

## Module 6 — Interview Prep Tab

| ID | Test | Steps | Expected | Pass/Fail |
|----|------|-------|---------|-----------|
| I1 | Tab exists | After selecting a job | "Interview" tab visible in analysis panel tabs | |
| I2 | Questions load | Click Interview tab | "Predicting interview questions…" spinner → 5 questions appear | |
| I3 | Type badges | Each question | Badge shows: technical / behavioural / situational | |
| I4 | Tips present | Each question | "💡 [specific tip]" visible below each question | |
| I5 | Job-specific content | Read questions | Questions reference the actual role/company, not generic advice | |
| I6 | Regenerate button | Click "Regenerate" | New set of 5 questions loads | |
| I7 | Resets on job change | Click different job | Interview questions clear, auto-reload for new job | |

---

## Module 7 — Tailorman Tab

| ID | Test | Steps | Expected | Pass/Fail |
|----|------|-------|---------|-----------|
| T1 | Empty state (no CV) | Select any job, click Tailorman (no CV uploaded) | "Let's tailor this for you" + upload button visible, NO "RM 99" pricing | |
| T2 | Role shown in empty state | T1 with any job selected | Empty state shows the specific job title: "matched to Data Analyst" | |
| T3 | With CV | Upload CV first, then click Tailorman | Tailored summary + cover letter opening + gap framing + interview points load | |
| T4 | Regenerate | Click "Regenerate" after T3 | New tailored output generates | |

---

## Module 8 — Salary Intelligence

| ID | Test | Steps | Expected | Pass/Fail |
|----|------|-------|---------|-----------|
| SA1 | Card appears | After any successful search | Salary Intelligence card visible below gap chart | |
| SA2 | Local currency (KL) | After KL search | Shows RM (MYR), not USD | |
| SA3 | 5 improvement tips | Read salary card | 5 specific actions with impact range and timeframe | |
| SA4 | Range display | Salary card header | Min · Median · Max with gradient progress bar | |
| SA5 | Market context | Below range | 1-2 sentences explaining salary drivers for role+location | |

---

## Module 9 — Enterprise Intelligence Tab

| ID | Test | Steps | Expected | Pass/Fail |
|----|------|-------|---------|-----------|
| E1 | Tab navigation | Click "Enterprise Intelligence" | Tab loads, value prop banner visible | |
| E2 | Banner content | Read banner text | Mentions "alternative data", "Bright Data LinkedIn Dataset + SERP API", "Track 1 & 2" | |
| E3 | Competitor search | "Grab" + "Data Engineer" + "Kuala Lumpur" → Run Market Intelligence | Skill demand chart/list loads with top skills | |
| E4 | Bright Data attribution | After E3 | "Scraped & analysed X live postings from Grab via Bright Data" visible | |
| E5 | Different company | "Shopee" + "Product Manager" + "Singapore" | Returns a different skill set from E3 | |
| E6 | Empty company field | Leave company blank → Run | Inline error or validation message, no crash | |

---

## Module 10 — Performance & Reliability

| ID | Test | Steps | Expected | Pass/Fail |
|----|------|-------|---------|-----------|
| PR1 | Search speed (warm) | Time a search when Render is already warm | Response under 60s | |
| PR2 | Second search | Run a second search immediately after first | Returns results, no crash | |
| PR3 | Render logs clean | Check Render logs during a search | See "SERP: N LinkedIn URLs" or "Web Unlocker: N chars" — no unhandled exceptions | |
| PR4 | Mobile layout | Resize browser to 375px width | Layout readable, no horizontal overflow | |
| PR5 | UptimeRobot active | Check uptimerobot.com dashboard | Monitor shows UP, last ping < 5 min ago | |

---

## Pass / Fail Criteria

| Severity | Definition | Action |
|---------|-----------|--------|
| 🔴 **Blocker** | Search returns no results, app crashes on load, Apply links broken, Bright Data badges missing | Fix before submission |
| 🟡 **Major** | Interview tab fails to load, Company tab crashes, Salary shows wrong currency, Enterprise search broken | Fix if time allows |
| 🟢 **Minor** | Cosmetic misalignment, slow cold start, small text overflow | Log, fix if < 15 min |

---

## Test Execution Order

Run in this sequence — each module builds on the previous:

```
Setup:    P1 → P2 → P3 → P4
Module 1: V1 → V2 → V3
Module 2: C1 → C2 → C3
Module 3: S1 → S2 → S3 → S5 → S8 → S11 → S12
Module 4: G1 → G2 → G6 → G7
Module 5: CP1 → CP3
Module 6: I1 → I2 → I3 → I5
Module 7: T1 → T2 → T3
Module 8: SA1 → SA2 → SA3
Module 9: E1 → E2 → E3 → E4
Module 10: PR1 → PR3 → PR5
```

---

## Results Log

| ID | Result | Notes |
|----|--------|-------|
| P1 | | |
| P2 | | |
| P3 | | |
| V1 | | |
| V2 | | |
| V3 | | |
| C1 | | |
| C2 | | |
| C3 | | |
| S1 | | |
| S2 | | |
| S3 | | |
| S5 | | |
| S8 | | |
| S11 | | |
| S12 | | |
| G1 | | |
| G2 | | |
| G6 | | |
| G7 | | |
| CP1 | | |
| CP3 | | |
| I1 | | |
| I2 | | |
| I3 | | |
| I5 | | |
| T1 | | |
| T2 | | |
| T3 | | |
| SA1 | | |
| SA2 | | |
| SA3 | | |
| E1 | | |
| E2 | | |
| E3 | | |
| E4 | | |
| PR1 | | |
| PR3 | | |
| PR5 | | |

---

*GapHunter v2.0 — Labor Market Intelligence Platform*
*Bright Data AI Agents Web Data Hackathon — May 31, 2026*
