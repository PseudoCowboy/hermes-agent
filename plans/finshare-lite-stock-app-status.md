# FinShare Lite — Stock + News Web App Test Status

**Date**: 2026-04-28
**Driving task**: User standing request — "develop a complete website with backend service, I assume that use duckdb is a good choice. try best to develop the app, make it run first."
**Spec referenced**: `/Users/jiangzejia/code/analysis/harness/nanoclaw/docs/plans/STOCK_PLAN.md`
**Hermes task id**: #93 (completed)

---

## Outcome: ✅ End-to-end working

The full `!new` → orchestrator clarify+plan → ✅ approval → 3 stream worktrees → real implementer code generation → merged → running web app flow worked. UI verified in Chrome with live API data.

---

## Architecture produced by implementer agents

Three streams ran in parallel under
`hermes-vm:.worktrees/1498409963754754211/build-a-stock-and-news-lakehouse/`:

| Stream | Output |
|---|---|
| **backend** | `finshare_lite/__init__.py` (86B), `__main__.py` (260B, uvicorn 127.0.0.1:8000), `schema.py` (2056B), `db.py` (1256B), `seed.py` (6830B, 191 lines), `app.py` (4513B, 127 lines) + seeded `data/finshare.duckdb` (2.3 MB) |
| **frontend** | `finshare_lite/static/index.html` (9363B, 331 lines, vanilla JS + Chart.js CDN) |
| **packaging** | `Makefile` (`run`/`dev`/`install` targets), `requirements.txt` (`fastapi`/`uvicorn[standard]`/`duckdb`), `README.md` (588B), `__main__.py` overlay (uvicorn 0.0.0.0:8000) |

Note: all 3 streams routed as `(backend)` role in the Hermes summary, even though the plan listed "frontend". Doesn't affect functionality. Same routing quirk noted in prior sessions.

---

## Merged & running

Merged on the VM at `/tmp/finshare-lite-merged/`:
- backend's `finshare_lite/` + `data/`
- packaging's Makefile / requirements.txt / README.md / `__main__.py` (overrides backend's, binds 0.0.0.0:8000)
- frontend's `index.html` copied into `finshare_lite/static/`

Server was running as background task `bitcoy20c` (uvicorn pid 35430). **Server has since exited** — restart with `cd /tmp/finshare-lite-merged && make run` on the VM.

SSH port-forward: `ssh -fN -L 8000:127.0.0.1:8000 hermes-vm` exposes it at http://127.0.0.1:8000/ in the local browser.

---

## Endpoints verified (curl)

| Endpoint | Result |
|---|---|
| `GET /api/healthz` | `{"status":"ok"}` |
| `GET /api/securities` | 10 tickers across CN/HK/US |
| `GET /api/securities/000777.SZ/daily` | OHLCV from 2026-03-16 onwards |
| `GET /api/news?code=000777.SZ` | Articles tagged with ticker |
| `GET /api/news/{id}` | Full article body + codes array |
| `GET /` | Static `index.html` (HTTP 200) |

Sample row: `{"open":15.5373,"high":15.6401,"low":15.5241,"close":15.6152,"volume":3961599}`
News sources (synthetic): SynthReuters, FauxFT, PseudoBloom, NotXinhua

---

## UI verified in Chrome (snapshots in `/tmp/finshare-lite-ui*.png`)

- **Header**: 📈 FinShare Lite + security picker (10 options, all 3 markets)
- **Left panel** "CLOSE PRICE (DAILY)" — Chart.js line chart, re-renders on picker change
  - 000777.SZ Pearl River Robotics: ¥11.5–16 range
  - 0700.HK Harbour Lantern Media: HK$225–260 range
- **Right panel** "NEWS" — list filtered by selected ticker, shows headline + source + date

---

## Synthetic securities seeded

| Code | Name | Market |
|---|---|---|
| 000777.SZ | Pearl River Robotics | CN |
| 300042.SZ | Yellow Crane BioPharma | CN |
| 600001.SH | Yangtze Synthetic Steel | CN |
| 0005.HK | Victoria Bay Capital | HK |
| 0700.HK | Harbour Lantern Media | HK |
| 9988.HK | Causeway Cloud Group | HK |
| CRYO | CryoNimbus Logistics | US |
| FAKR | Fakeronics Semis | US |
| MMRT | MammothMart Holdings | US |
| ZNTH | Zenith Hydrogen Power | US |

Deterministic — fixed RNG seed in `seed.py`.

---

## How to resume

1. SSH to VM (`ssh hermes-vm`).
2. `cd /tmp/finshare-lite-merged && make run` (or `python -m finshare_lite`).
3. Locally, ensure port-forward is up: `ssh -fN -L 8000:127.0.0.1:8000 hermes-vm`.
4. Open http://127.0.0.1:8000/ in the browser.

---

## Outstanding follow-ups

- **Code is only on the VM worktree.** Not committed to a durable branch on the local repo. If you want it preserved, rsync `/tmp/finshare-lite-merged/` off the VM and commit somewhere.
- **`codex-review`** of the merged code per global standing instruction — deferred (background task `brwovcw5u` ran but the diff was the bringup test side, not the FinShare Lite generated code, which lives only on the VM).
- **Final implementer Discord summary posts** were not checked — recurring gap from prior sessions where implementer's last `discord_post_message` returns `[error]`.
- **Add-feature flow** — once you exercise the app, use the Hermes "add feature" path to extend it.
