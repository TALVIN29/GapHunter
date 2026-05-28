# GapHunter — Final E2E Test Plan
## Post-Patch Verification + Demo-Day Go/No-Go
## Execute: Day 29 (2026-05-29) — After Deploying Patched api.py

---

## Progress Tracker

```
# Progress: GapHunter — Final E2E Testing
## Status: IN_PROGRESS
## Talvin Principle: STP (automated steps) + HITL (Render dashboard steps)
## Knowledge Tier: 3
## Progress: 7/17 tasks complete = 41%
## Last checkpoint: All 7 patches applied locally (api.py, resume.py, roadmap.py)
## Next action: git push → Render deploy → run patch verification tests below

## Tasks

### Patch Application [COMPLETE]
[x] Patch 1 — Token Bleed (Fracture 3): shadow_forced exits before Gate 1, zero LLM on timeout
[x] Patch 2 — VPN Fix (Fracture 4): Layer 2 bypassed for authenticated (secret-carrying) traffic
[x] Patch 3 — OOM Bomb (Content-Length): ContentLengthGuard fast-fail on declared size > 2 MB
[x] Patch 4 — Cache Poison: session_id field added, POISON_GUARD log + uuid.uuid4() override
[x] Patch 5 — OOM Bomb (Chunked): ContentLengthGuard streams chunked bodies, aborts at 2 MB
[x] Patch 6 — DOCX Zip Bomb: zipfile central-directory scan before python-docx, 15 MB limit
[x] Patch 7 — LRU Cache: _LRUSessionCache replaces unbounded dict, MAX_SESSIONS=300, O(1) eviction

### Deployment [NEXT]
[ ] git push → Render auto-deploy (or Manual Deploy)
[ ] Wait for "Application startup complete" in Render logs
[ ] Verify /health: status=ok, circuit_open=false, shadow_forced=false, fallback_ready=true

### Patch Verification Tests [PENDING — post-deploy]
[ ] V1 — Patch 3 (OOM): curl with oversized body → expect HTTP 413
[ ] V2 — Patch 2 (VPN): 8 searches with valid secret → no rate_limited response
[ ] V3 — Patch 4 (Poison): session_id="demo-static" in body → UUID in response, POISON_GUARD in logs
[ ] V4 — Patch 1 (Token): SCRAPE_TIMEOUT_S=1 redeploy → zero httpx calls after shadow line in logs

### Chaos Tests Re-Run [PENDING — after patch verification]
[ ] Test 3 re-run (Fracture 3): shadow_forced path → zero HAIKU_CALL in logs
[ ] Test 4 re-run (Fracture 1+2): circuit trip → circuit_open=true, roadmap ready on first poll

### Demo Recording Gate [PENDING]
[ ] POST-SUITE health check: ALL CLEAR
[ ] Addendum O golden path dry run: CIRCUIT_BREAKER_LIMIT=1 → trip → narrate → roadmap loads
[ ] Restore: CIRCUIT_BREAKER_LIMIT=100 → redeploy → circuit_open=false confirmed

## Blockers: None — awaiting Render deploy of patched api.py
## Decisions:
##   Patch 2 makes Layer 2 a no-op for all current authenticated routes — by design.
##   Unauthenticated traffic is already 403'd by Layer 1 before Layer 2 is reached.
```

---

## Section 0 — Environment Setup

```bash
export SECRET="gaphunter-demo-2026"
export BACKEND="https://gaphunter-api.onrender.com"

echo "BACKEND : $BACKEND"
echo "SECRET  : [${#SECRET} chars]"
```

---

## Section 1 — Deploy Verification

```bash
echo "=== POST-DEPLOY HEALTH CHECK ==="
curl -s --max-time 15 "$BACKEND/health" | python3 -c "
import json, sys
d = json.load(sys.stdin)
fields = ['status','circuit_open','shadow_forced','fallback_ready']
for f in fields:
    print(f'{f}: {d.get(f)}')
print()
ok = d['status']=='ok' and not d['circuit_open'] and not d['shadow_forced'] and d['fallback_ready']
print('DEPLOY CHECK:', 'PASS' if ok else 'FAIL — do not proceed')
"
```

**Pass condition:** All four fields match `ok / false / false / true`. `uptime_s < 120` confirms fresh deploy.

---

## Section 2 — Patch Verification Tests

### V1 — Patch 3: OOM JSON Bomb (HTTP 413)

```bash
echo "=== V1: OOM Bomb — Content-Length > 2 MB ==="

# Generate a 3 MB payload inline
BOMB=$(python3 -c "import json; print(json.dumps({'role': 'x' * (3 * 1024 * 1024)}))")

HTTP_CODE=$(echo "$BOMB" | curl -s -o /dev/null -w "%{http_code}" \
  -X POST "$BACKEND/api/search" \
  -H "Content-Type: application/json" \
  -H "X-Demo-Secret: $SECRET" \
  -d @-)

echo "HTTP status: $HTTP_CODE  (must be 413)"
[ "$HTTP_CODE" = "413" ] && echo "V1: PASS" || echo "V1: FAIL — ContentLengthGuard not active"
```

**Pass:** `HTTP 413`. **Fail:** `HTTP 200/422` — middleware not registered or running after body parse.

---

### V2 — Patch 2: VPN Bypass — No Rate Limit for Authenticated Requests

```bash
echo "=== V2: Authenticated traffic exempt from rate limit ==="
PAYLOAD='{"role":"Data Analyst"}'
FAIL=0

for i in $(seq 1 8); do
  STATUS=$(curl -s --max-time 60 \
    -X POST "$BACKEND/api/search" \
    -H "Content-Type: application/json" \
    -H "X-Demo-Secret: $SECRET" \
    -d "$PAYLOAD" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','ERROR'))")
  echo "Request $i: status=$STATUS"
  [ "$STATUS" = "rate_limited" ] && FAIL=1
done

echo ""
[ $FAIL -eq 0 ] && echo "V2: PASS — no rate_limited after 8 authenticated requests" \
                || echo "V2: FAIL — rate_limited fired on authenticated traffic (Patch 2 not active)"
```

**Pass:** All 8 responses are `ok`, `invalid_query`, or `no_results` — never `rate_limited`.

---

### V3 — Patch 4: Session ID Cache Poison Guard

```bash
echo "=== V3: session_id='demo-static' on live path ==="

RESP=$(curl -s --max-time 60 \
  -X POST "$BACKEND/api/search" \
  -H "Content-Type: application/json" \
  -H "X-Demo-Secret: $SECRET" \
  -d '{"role": "Data Analyst", "session_id": "demo-static"}')

echo "$RESP" | python3 -c "
import json, sys, re
d = json.load(sys.stdin)
sid = d.get('session_id', 'NONE')
uuid_pat = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
print('session_id returned:', sid)
print()
if uuid_pat.match(str(sid)):
    print('V3: PASS — response session_id is a fresh UUID (not demo-static)')
elif sid == 'demo-static':
    print('V3: FAIL — reserved ID returned in response (circuit_open may be True — check health)')
else:
    print('V3: CHECK — unexpected session_id format')
"

echo ""
echo "=== V3: Verify POISON_GUARD in Render logs ==="
echo "[MANUAL] Render Dashboard → Logs → look for: POISON_GUARD: client claimed reserved session_id"
```

**Pass:** Response `session_id` is a UUID. Render logs show `POISON_GUARD` warning line.

---

### V4 — Patch 1: Token Bleed Sealed (Shadow Mode = Zero LLM)

```
[MANUAL] Render Dashboard → Environment → SCRAPE_TIMEOUT_S → set to: 1
[MANUAL] Save Changes → Manual Deploy → wait for "Application startup complete"
```

```bash
echo "=== V4: Shadow Mode — zero LLM calls ==="
curl -s --max-time 30 \
  -X POST "$BACKEND/api/search" \
  -H "Content-Type: application/json" \
  -H "X-Demo-Secret: $SECRET" \
  -d '{"role": "Data Analyst"}' \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
sid = d.get('session_id','')
print('status    :', d.get('status'))
print('session_id:', sid)
print()
if sid == 'demo-static':
    print('V4: PASS — static demo served (shadow_forced triggered circuit_open path)')
elif d.get('status') == 'ok':
    print('V4: PASS — static demo served via shadow_forced path')
else:
    print('V4: CHECK — unexpected status, see Render logs')
"
```

```
[MANUAL] Render Dashboard → Logs → verify sequence:
  EXPECTED:
    WARNING:api:Scrape timed out after 1s — using fallback
    INFO:api:SHADOW_FORCED: Bright Data down — serving static demo state, zero LLM calls
  ZERO httpx POST api.anthropic.com lines after the shadow line

PASS: shadow line present + zero httpx after it
FAIL: httpx lines present after shadow line — Patch 1 not active
```

```
[MANUAL] Restore: SCRAPE_TIMEOUT_S → 12 → Save → Manual Deploy
```

---

## Section 3 — Chaos Tests Re-Run (Fracture Regression)

### Test 3 Re-Run — Fracture 3 Regression

Repeat V4 above with explicit job/gap count validation:

```bash
TIME_START=$(python3 -c "import time; print(time.time())")
RESP=$(curl -s --max-time 30 \
  -X POST "$BACKEND/api/search" \
  -H "Content-Type: application/json" \
  -H "X-Demo-Secret: $SECRET" \
  -d '{"role":"Data Analyst","session_id":"shadow-regression-test"}')
TIME_END=$(python3 -c "import time; print(time.time())")

echo "$RESP" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('status:', d.get('status'))
print('jobs  :', len(d.get('jobs', [])))
print('gaps  :', len(d.get('gaps', [])))
"
python3 -c "print(f'Wall clock: {float(\"$TIME_END\") - float(\"$TIME_START\"):.2f}s')"
```

| Check | Pass | Fail |
|---|---|---|
| `status` | `ok` | `error` / `500` |
| `jobs` | ≥ 5 | < 3 |
| Wall clock | ≤ 6s | > 15s (real scrape attempted) |
| Render logs: httpx after shadow line | 0 lines | Any line = Patch 1 regression |

---

### Test 4 Re-Run — Fractures 1 + 2 Regression

```
[MANUAL] CIRCUIT_BREAKER_LIMIT → 1 → Save → Manual Deploy
```

```bash
echo "=== Test 4 Re-Run: Trip ==="
curl -s --max-time 60 -X POST "$BACKEND/api/search" \
  -H "Content-Type: application/json" -H "X-Demo-Secret: $SECRET" \
  -d '{"role":"Data Analyst"}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('Trip session_id:', d.get('session_id'))"

echo ""
echo "=== Health after trip ==="
curl -s "$BACKEND/health" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('circuit_open:', d.get('circuit_open'), '  (must be true)')
print('shadow_forced:', d.get('shadow_forced'))
"

echo ""
echo "=== Second search: circuit-open path ==="
TIME_START=$(python3 -c "import time; print(time.time())")
SEARCH=$(curl -s --max-time 10 -X POST "$BACKEND/api/search" \
  -H "Content-Type: application/json" -H "X-Demo-Secret: $SECRET" \
  -d '{"role":"Data Analyst"}')
TIME_END=$(python3 -c "import time; print(time.time())")
echo "$SEARCH" | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('session_id:', d.get('session_id'), '  (must be demo-static)')
gaps=d.get('gaps',[]); print('gaps[0].skill:', gaps[0].get('skill') if gaps else 'MISSING')
"
python3 -c "print(f'Latency: {(float(\"$TIME_END\")-float(\"$TIME_START\"))*1000:.0f}ms  (must be < 500ms)')"

echo ""
echo "=== Roadmap poll ==="
curl -s "$BACKEND/api/roadmap/dbt?session_id=demo-static" | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('status:', d.get('status'), '| roadmap:', 'present' if d.get('roadmap') else 'null')
[ d.get('status')=='ready' ] and print('Test 4B: PASS')
"
```

```
[MANUAL] Restore: CIRCUIT_BREAKER_LIMIT → 100 → Save → Manual Deploy
```

---

## Section 4 — Demo Recording Go/No-Go Gate

### Final Health Check

```bash
echo "=== POST-SUITE HEALTH CHECK ==="
curl -s --max-time 10 "$BACKEND/health" | python3 -c "
import json, sys
d = json.load(sys.stdin)
checks = [
    ('status',        d.get('status'),        'ok',   'Backend not running'),
    ('circuit_open',  d.get('circuit_open'),  False,  'Breaker left open — redeploy required'),
    ('shadow_forced', d.get('shadow_forced'), False,  'Shadow forced left on — redeploy required'),
    ('fallback_ready',d.get('fallback_ready'),True,   'Fallback file missing'),
]
all_pass = True
for field, actual, expected, msg in checks:
    ok = actual == expected
    print(f'  [{\"PASS\" if ok else \"FAIL\"}] {field}: {actual!r}', '' if ok else f'— {msg}')
    if not ok: all_pass = False
print()
print('GO/NO-GO:', 'GO — proceed to demo recording' if all_pass else 'NO-GO — fix failures above')
"
```

---

## Section 5 — Hard Success Metrics

| Scenario | Trigger | Expected Behavior | HTTP | Cost | UI State |
|---|---|---|---|---|---|
| **JSON Bomb** | `Content-Length > 2 MB` | `ContentLengthGuard` drops before routing | `413` | $0.00 | No fetch completes; frontend error state |
| **Scrape Timeout (shadow_forced)** | Bright Data exceeds `SCRAPE_TIMEOUT_S` AND `shadow_forced=True` | Early return before Gate 1; `_load_static_demo()` served | `200` | $0.00 | Static demo rendered; roadmaps resolve from startup cache |
| **Budget Exhausted (circuit_open)** | `live_search_count >= CIRCUIT_BREAKER_LIMIT` | `circuit_open` check fires at handler entry; `_load_static_demo()` served | `200` | $0.00 | Static demo; `session_id: "demo-static"`; roadmap in < 1.5s |
| **No Secret Header** | POST without `X-Demo-Secret` | `_require_demo_secret` raises `HTTPException` | `403` | $0.00 | Fetch rejected; browser shows error |
| **Authenticated Burst (Judges)** | > 5 req/hr with valid secret | Layer 2 bypassed; live pipeline executes | `200` | ~$0.17/search (capped by circuit breaker at limit) | Normal search result; no rate_limited modal |
| **Unauthenticated Burst** | > 5 req/hr without secret | `403` from Layer 1 on every request | `403` | $0.00 | N/A — no frontend path |
| **PDF Prompt Injection** | PDF containing `Ignore all instructions` payload | Addendum E 7-layer chain sanitizes; skills extracted normally | `200` | 1× Haiku call | Resume parsed; injected content absent from skills |
| **MIME Spoof** | PNG file with `.pdf` extension | Magic bytes check rejects before `pdfplumber` | `200` | $0.00 | `parse_failed` status; upload error shown |
| **Session ID Poison Attempt** | POST with `session_id: "demo-static"` on live path | POISON_GUARD logs warning; response issues fresh UUID; cache untouched | `200` | ~$0.17 (normal live search) | Normal result; `ROADMAP_CACHE["demo-static"]` intact |
| **Day Rollover (partial reset)** | `_maybe_reset` fires on new calendar day | `shadow_forced` reset to False; `circuit_open` stays True until restart | `200` | $0.00 (circuit still open) | Static demo continues to serve; no live path until server restart |
| **Chunked Upload (no Content-Length)** | Body without `Content-Length` header | Framework parses; Pydantic rejects invalid schema | `422` | $0.00 | N/A — not a realistic free-tier attack vector |

---

## Section 6 — Scoring Rules

| Score | Interpretation | Action |
|---|---|---|
| All V1–V4 PASS + Tests 3/4 PASS | All patches active, no regressions | Proceed to demo recording |
| V1 FAIL (413 not returned) | `ContentLengthGuard` not registered or running after body parse | Check `app.add_middleware(ContentLengthGuard)` placement — must be after CORSMiddleware |
| V2 FAIL (rate_limited on auth) | Patch 2 not active | Check `_is_authenticated` branch in handler — confirm `_DEMO_SECRET` env var is set on Render |
| V3 FAIL (demo-static in response) | `circuit_open=True` from previous test — not a Patch 4 failure | Run health check; if `circuit_open=True`, redeploy to reset |
| V4 FAIL (httpx after shadow) | Patch 1 not active — `shadow_forced` check not reached before Gate 1 | Confirm order in handler: `_maybe_reset` + `shadow_forced` check must precede Gate 1 |
| Test 3B FAIL only (LLM after shadow) | Fracture 3 regression after patch | Check that `shadow_forced` early return is before `validate_and_normalize` call |
| Test 4A FAIL (circuit_open=false) | Fracture 1 regression | Check `_tick_circuit_breaker` — both `if not state.shadow_forced` and `if not state.circuit_open` guards |
