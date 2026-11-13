# DOGFOOD — Mishpat

## 2026-11-12 — idle-reaper safety hardening (branch `feat/idle-reaper`)

Goal: the 60s idle auto-close must never silently destroy a session mid-use,
never hand a caller a dead/None driver, and never orphan chromedriver+chromium
when the MCP server exits — while keeping auto-close for abandoned sessions.

### Scores (strict gate — overall = minimum, not average)

| Surface | Score | Evidence |
|---|---|---|
| REPL `shell` (incl. new `idle` cmd) | 10/10 | piped session: `idle` printed live dict (alive/idle_timeout/relaunches), `ASSERT_PASS`, quit closed; shots/shot-1112-* |
| scripted `run` | 10/10 | `run tests/demo.json` → PASS 3/3 (code channel) + per-step fresh screenshots (user channel) |
| one-shot `screenshot` | 10/10 | rc=0, fresh PNG written |
| MCP server (16 tools) | 10/10 | fastmcp client: goto/idle/quit/relaunch/assert all PASS; PID-diff proof: server exit leaves no chromedriver behind (+12s settle) |
| idle-reaper lifecycle | 10/10 | repro→fix pairs: `wait` > ttl used to relaunch `data:,` → now survives with URL intact; goto vs 7s-slow page died `InvalidSessionIdException` → now `GOTO-SURVIVED`; 3-thread reaper-vs-use stress 0 errors; reaper still closes genuinely idle sessions (detected closed at 4.0s for ttl=3, tick 0.75s) |
| `record` | 10/10 | shots/dogfood-record.mp4, ffprobe duration=2.0s, rc=0 |
| `helpful` (headed, WSLg `:0`) | 10/10 | `idle` printed the live status dict in-session; quit stitched shots/dogfood-helpful.mp4, rc=0 |
| pyright (changed files) | 10/10 | 1 error total = pre-existing main.py:23 `reconfigure`, untouched by this diff |

### Bug class

"Session state silently stops meaning what the command output claims" — any
wait/long-op path could be reaped mid-use while the next call kept reporting
success on a blank page. Sites found and closed: `steps.py` wait, `main.py`
shell wait, `main.py` helpful wait, `agent.py` wait, `wait_for`/`wait_idle`
poll loops, MCP exit path, `_require` reaper TOCTOU.

### Tests

- Baseline: repo defines no unit suite (`tests/` = JSON scripts) and no typecheck; baseline check = `run tests/demo.json` 3/3.
- After: same suite green + proof battery (`repro_m1`, `repro_a1`, `rebut_a1prime`, `verify_fix`, `verify_reaper`, `verify_n`, `mcp_smoke`) — all PASS; pyright 0 new findings.

### Creative tests (not asked for — where the bugs were)

- Local HTTP server delaying 7s vs `idle_timeout=1` → caught the mid-goto reaper kill (finding A1).
- 3-thread hammer against the reaper → caught the None-driver TOCTOU (fixed with the RLock handoff).
- First-use phantom `relaunches: 1` on a fresh session (finding N2).
- Nested-quit join latency under a held lock (finding N3: 2.06s → 0.46s).

### Gaps (honestly unmet)

- `agent` LLM loop not run live (no `OPENAI_API_KEY`/`.env` in this workspace); its wait fix is proven at the `_exec` level — the exact call the loop makes.
- SIGKILL of the MCP server (external supervisor restart) cannot run Python `finally` cleanup; the reaper covers idle-while-alive only. Related jury waiver: M9 (CLI `DRIVER_LOST` exits skip `quit()`, pre-existing).
- Jury waivers accepted as written: M8 (60s default applies to headed sessions), M10 (`quit()` waits behind an in-flight op), W1 (`_is_owned()` is a CPython-internal API), W2 (one observed boaz-suite flake, passed on rerun + isolation).

### Branch

`feat/idle-reaper` — PR filed from this branch.
