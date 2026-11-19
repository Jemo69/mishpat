# DOGFOOD — Mishpat

## 2026-11-18 — mobile emulation + agent DevTools (branch `feat/mobile-devtools`)

Goal: the agent sees the site as a phone (device/viewport/rotate, --device)
and uses Chrome DevTools headless (network/perf/throttle/theme) across REPL,
scripts, MCP, and the agent loop. Headless stays the contract; one headed
`helpful` run only to prove the shared dispatcher both loops now use.

### Scores (strict gate — overall = minimum, not average)

| Surface | Score | Evidence |
|---|---|---|
| mobile emulation | 10/10 | `run tests/mobile.json` 13/13 (innerWidth===412 + Pixel UA post-goto, 915 post-rotate, non-Pixel post-desktop); fresh `screenshot --device iphone-15-pro` PNG 1179x2556 (393x852 @3x); maxTouchPoints=1; `record --device pixel-8` MP4 7.0s |
| devtools (network/perf/throttle/theme) | 10/10 | `run tests/devtools.json` 11/11 (onLine false/true across offline round-trip); 404 path surfaces status in failed[]; throttle+theme survive 2s-ttl reaper relaunch; http->https redirect shows final URL |
| shared REPL dispatch | 10/10 | piped `shell`: bare `device` listing, device switch, network, perf, unknown-cmd, quit (shot dict printed once); one headed `helpful` (DISPLAY=:0): pixel-8 + network + MP4 stitch 2 frames |
| MCP (23 tools) | 10/10 | live `tools/list` = 23 (no browser_devices); live calls: goto, device pixel-8, network (2 reqs), bare device listing |
| regression + isolation | 10/10 | `run tests/demo.json` 3/3; parallel ports 19554 (mobile 13/13) + 19555 (demo 3/3) PASS simultaneously — per-instance emulation state, no cross-talk; pyright 1 error = pre-existing reconfigure; zero orphan chrome/chromedriver |

### Bug class

"CDP/session state silently stops meaning what the command claims" — review
rounds found 6 live instances and every one is fixed + proven: stale mobile UA
on desktop switchback (C1: always-set UA override), throttle/theme lost on
reaper relaunch (C2: stored + reapplied), resize clearing the metrics override
(emulate() orders resize-before-override), swallowed CDP errors (throttle/theme
raise RuntimeError), redirect-stale network URLs (C4: follow final hop),
silent js default on agent text (normalize maps text->expression).

### Tests

- Baseline: `run tests/demo.json` 3/3 + `run tests/mobile.json` 9/9 +
  `run tests/devtools.json` 8/8 on the first cut (asserts added after R1).
- After: mobile 13/13, devtools 11/11, demo 3/3, REPL smoke, MCP live calls,
  parallel isolation, UA-swap + relaunch persistence checks — all PASS.
- Jury: 2-model panel (opencode/Hy4 simplicity + copilot/GPT correctness),
  3 rounds to double 10/10 (R1 both CHANGES 8/10, R2 10/10 + 9/10, R3 double 10/10).

### Gaps (honestly unmet)

- `agent` LLM loop not run live (no OPENAI_API_KEY in this workspace); its
  _exec delegation is proven at the unit level (network/device/viewport/WxH/
  throttle/js-text all return shot-backed dicts).
- SIGKILL/kill -9 driver-death path not re-proven live (pre-existing,
  untouched; a pkill probe risked the operator's own shell, aborted).
- Skill text under ~/.agents/skills/mishpat lives outside this repo — README
  updated here; skill sync is a follow-up.


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
