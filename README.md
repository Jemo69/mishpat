# Mishpat — headless Selenium CLI for AI agents

No visible browser. The agent opens a terminal, drives Chromium headless,
and reads screenshots to decide the next command.

## Setup (uv default)

```bash
git clone https://github.com/Jemo69/mishpat.git ~/projects/Mishpat
cd ~/projects/Mishpat
uv sync
cp .env.example .env   # only needed for `agent` mode
# optional global shim (~/.local/bin/mishpat):
#   #!/bin/sh
#   exec uv run --project /home/jemo/projects/Mishpat /home/jemo/projects/Mishpat/main.py "$@"
```

Requires: Chromium + chromedriver (already at `/usr/bin/chromium`, `/usr/bin/chromedriver`).

## The 4 modes

```bash
# 1. REPL — the flow you asked for: agent opens terminal, screenshots, continues
uv run python main.py shell --url https://example.com
mishpat> snapshot
mishpat> screenshot            # -> shots/shot-001.png (agent reads this file)
mishpat> click "a"
mishpat> type "input[name=q]" "hello"
mishpat> assert "documentation"
mishpat> quit

# 2. One-shot screenshot
uv run python main.py screenshot https://example.com --out shots/shot.png

# 3. Scripted test (repeatable)
uv run python main.py run tests/demo.json

# 4. Autonomous agent — LLM dynamically picks commands, screenshot feeds back each step
uv run python main.py agent --goal "verify docs link works" --url https://example.com --max-steps 10

# 5. Record video (local ffmpeg, free — no paid service)
uv run python main.py record --url https://example.com --script tests/demo.json --out shots/demo.mp4
uv run python main.py record --url https://example.com --out shots/tour.mp4   # auto-scroll tour

# 6. Helpful mode — VISIBLE browser + guided REPL, stitches MP4 on quit (for video recording)
uv run python main.py helpful --url https://example.com --video-out shots/helpful.mp4

# 5. MCP server — "browser-use without opening a browser" for opencode/pi
uv run python main.py mcp
```

Script format (`tests/demo.json`) — same verbs in `run`, `record`, `shell`, `helpful`, MCP, agent:
```json
[
  {"action": "goto", "url": "https://example.com"},
  {"action": "click_text", "text": "Learn more"},
  {"action": "wait", "seconds": 3},
  {"action": "assert", "text": "documentation"},
  {"action": "console"},
  {"action": "tabs"}
]
```
Verbs: `goto snapshot click click_text type press scroll wait wait_for wait_idle js back forward reload dom console tabs tab checkpoint restore screenshot assert`.

Agent accelerators: `js <expr>` (set localStorage tokens, read state — skips the 60s UI login), `checkpoint <file>` / `--restore` (resume logged-in sessions), `wait_for <js>` + `wait_idle` (conditions over blind sleeps; `wait_idle` after `goto` guards pre-hydration native submits), `click [N]` (snapshot refs beat hostile CSS), `--quiet` (paths only, parseable stdout), `--driver-port N` (parallel-run isolation), `ASSERT_PASS/FAIL` tokens, `DRIVER_LOST` + exit 2 on driver death.

REPL extras: `dom raw` (unwrapped HTML for grep), `tab <n>` (jump tabs — clicks auto-follow `_blank`), `console` (JS errors in-terminal). Shots are session-prefixed (`shot-MMDD-HHMMSS-001.png`) so runs never overwrite each other. Typing hidden inputs fails loudly; stale-element races retry once; errors are one-liners.

## Give the agent browser-use with no visible window

Wired on this machine for all four coding CLIs (Claude Code, Codex, OpenCode, Pi):

| CLI | Skill | MCP |
|---|---|---|
| Claude Code | `~/.claude/skills/mishpat` (symlink) | `claude mcp add --scope user mishpat-headless -- uv run --project /home/jemo/projects/Mishpat /home/jemo/projects/Mishpat/main.py mcp` ✅ connected |
| Codex | `~/.codex/skills/mishpat` (symlink) | `[mcp_servers.mishpat-headless]` in `~/.codex/config.toml` |
| OpenCode | `~/.config/opencode/skills/personal/mishpat` (symlink) | `mishpat-headless` in `~/.config/opencode/opencode.json` |
| Pi | `~/.agents/skills/mishpat` (native) | `mishpat-headless` in `~/.pi/agent/mcp.json` |

Single source of truth: `/home/jemo/.agents/skills/mishpat/` — the rest are symlinks.

Manual OpenCode snippet (already applied above):

```json
{
  "mcp": {
    "mishpat-headless": {
      "type": "local",
      "command": ["/home/jemo/.local/bin/uv", "run", "--project", "/home/jemo/projects/Mishpat/", "/home/jemo/projects/Mishpat/main.py", "mcp"]
    }
  }
}
```

Tools exposed: `browser_goto, browser_snapshot, browser_click, browser_type, browser_screenshot, browser_dom, browser_assert, browser_back`.
All run headless (`--headless=new`); screenshots land in `shots/` for the agent to read.

## Where else to get this (public options)

| Option | Headless? | Notes |
|---|---|---|
| This repo (`mishpat`) | ✅ yes | Selenium, you own it |
| `@modelcontextprotocol/server-playwright` / Playwright MCP | ✅ yes (default) | `npx @modelcontextprotocol/server-playwright --headless`; most mature |
| `browser-use` python lib | ✅ yes (`headless=True`) | agent loop built-in, needs `OPENAI_API_KEY` |
| Your existing `browser-use` MCP (`computer-use/browser-use`) | ❌ no | drives real Chrome via extension — headed by design |
| Selenium MCP (various) | ✅ | thinner than Playwright, same idea as here |

You already have the headed half wired up. This adds the headless half.
