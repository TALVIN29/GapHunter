# GapHunter E2E Test Plan — v2.1
**Updated:** 2026-05-29 | **Deadline:** 2026-05-31 | **Environment:** Production

---

## Priority Order (run in this sequence)

```
BLOCKER tests first → MAJOR → MINOR
Stop and fix any BLOCKER before continuing.
```

---

## TIER 1 — Blockers (must pass before submission)

| ID | Test | Steps | Expected | Pass/Fail | Notes |
|----|------|-------|---------|-----------|-------|
| B1 | Search returns real jobs | "Data Analyst" + "Kuala Lumpur" → Run Agent | 5–10 LinkedIn jobs, data_source=live, <70s | | |
| B2 | Gap analysis requires skills | Click any job WITHOUT entering skills | Yellow warning: "Your skills are needed" + Upload CV / Enter skills CTA | | |
| B3 | Gap analysis with skills | Enter "Python, SQL, Excel" in skills field → search → click job | Gaps tab shows YOUR actual missing skills vs job, not generic list | | |
| B4 | CV upload auto-detects role | Upload real PDF/DOCX → check role field | Role field turns green, "Auto-detected from CV" badge, skills populated | | |
| B5 | Gap analysis after CV upload | After B4, click a job → Gaps tab | Shows real gaps based on CV-extracted skills | | |
| B6 | Apply links work | Click "Apply →" on any job | Opens real LinkedIn/job board URL in new tab | | |
| B7 | Location "KL" works | "Data Analyst" + "KL" → Run Agent | Returns Malaysia jobs, NOT California | | |
| B8 | Enterprise search works | Enterprise tab → "Grab" + "Data Engineer" + "KL" → Run Market Intelligence | Skill demand signal loads with real data | | |

---

## TIER 2 — Major (fix if time allows)

| ID | Test | Steps | Expected | Pass/Fail | Notes |
|----|------|-------|---------|-----------|-------|
| M1 | Progress steps visible | Watch during search | 6 steps advance every ~12s with Bright Data tool names | | |
| M2 | Bright Data attribution badges | After results load | "SERP API · LinkedIn · Browser · Claude AI" badges visible | | |
| M3 | Demand score formula visible | Scroll to gap chart | Formula card shows 5 signals with coloured labels | | |
| M4 | Interview prep tab | Click Interview tab on any job | 5 questions load with type badges (technical/behavioural/situational) | | |
| M5 | Tailorman with skills | Upload CV → click job → Tailorman tab | Tailored summary + cover letter + talking points load | | |
| M6 | Tailorman without skills | Click Tailorman with no CV | "Let's tailor this for you" + Upload CV prompt | | |
| M7 | Company tab | Click Company tab on Maybank or EPAM | Rating, pros/cons, culture summary visible | | |
| M8 | Salary intelligence | After any search | Salary card loads with local currency (RM for KL) | | |
| M9 | Roadmap accordion | Click any skill in Learning Roadmaps | Expands with steps, time estimate, resource links | | |
| M10 | Date format | Check posted date on any job | "3 days ago" not "2026-05-26T07:28:24.227Z" | | |

---

## TIER 3 — Minor (nice to have)

| ID | Test | Steps | Expected | Pass/Fail | Notes |
|----|------|-------|---------|-----------|-------|
| N1 | Empty role validation | Click Run Agent with nothing filled | Red border + shake on role field, no Swal popup | | |
| N2 | Mobile layout | Resize to 375px width | Single column, no horizontal scroll, readable | | |
| N3 | Particles background | Fresh load (no search) | Subtle particle animation visible in hero area | | |
| N4 | Typed.js hero | Fresh load | Typewriter text animating in centre | | |
| N5 | Agent pipeline strip | After search | ① Validate → ② SERP → ③ Scrape → ④ Extract → ⑤ Pre-fetch (highlighted when ready) | | |
| N6 | Job count / attribution | After search | "7 jobs · 9 postings analysed in Kuala Lumpur" + badge strip | | |

---

## Core Demo Flow (what judges will see)

Run this exact sequence as your hackathon demo:

```
1. Open GapHunter — show hero with typed.js animation + Bright Data branding
2. Upload a CV → watch role auto-detect + skills populate
3. Location: "Kuala Lumpur" → Run Agent
4. Show progress steps (names Bright Data tools)
5. Results load — point out: 8 real LinkedIn jobs, Live badge, attribution badges
6. Click "Data Engineer" → Gaps tab
   → Show YOUR actual skill gaps (computed from CV vs job requirements)
   → Point out demand score formula
7. Click Company tab → Glassdoor/Indeed data via Bright Data Web Unlocker
8. Click Interview tab → 5 AI-predicted questions
9. Click Tailorman → tailored CV summary + cover letter
10. Scroll down → Learning Roadmaps + Salary Intelligence
11. Switch to Enterprise tab → explain job postings as alternative data
12. Search "Grab" + "Data Engineer" + "KL" → show skill demand radar
```

---

## Known Issues (not blocking)

| Issue | Severity | Notes |
|-------|---------|-------|
| 7 vs 8 jobs variance | Minor | LinkedIn SERP varies per call — normal |
| Scraping Browser → same HTML as Unlocker for Jobstreet | Minor | Jobstreet is CSR, both fall back to Web Unlocker. Core LinkedIn pipeline works. |
| Search takes 45–70s | Minor | UptimeRobot keeps Render warm, cold starts avoided |
| Gap % low without skills | By design | Now shows warning instead of hallucinating |

---

## Pre-Submission Checklist

- [ ] B1–B8 all pass
- [ ] M1–M5 all pass  
- [ ] UptimeRobot pinging every 5 min (monitor ID: 803163282)
- [ ] DECISION_LOG.md updated (D-014 through D-020)
- [ ] Submission written on lablab.ai
- [ ] Demo video recorded (if required)
- [ ] `BRIGHTDATA_BROWSER_ZONE=scraping_browser1` in Render
- [ ] `SCRAPE_TIMEOUT_S=150` in Render (or removed)

---

*GapHunter v2.1 — Labor Market Intelligence Platform*  
*Bright Data AI Agents Web Data Hackathon — May 31, 2026*
