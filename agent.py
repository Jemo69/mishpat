"""Autonomous agent loop: LLM dynamically picks Selenium commands, screenshots feed back in."""
from __future__ import annotations

import json
import os
from typing import Any

import httpx
from dotenv import load_dotenv

from browser import HeadlessBrowser
from steps import do_step, short_error

load_dotenv()

SYSTEM_PROMPT = """You test web pages with headless Selenium. On each step reply with ONE JSON tool call:
{"action": "goto"|"snapshot"|"click"|"click_text"|"type"|"press"|"scroll"|"wait"|"wait_for"|"wait_idle"|"js"|"back"|"reload"|"dom"|"console"|"network"|"perf"|"device"|"viewport"|"rotate"|"throttle"|"theme"|"tabs"|"tab"|"screenshot"|"assert"|"done",
 "selector": "<css, for click/type>", "text": "<text/url/needle/device-name/WxH>", "pixels": 800, "seconds": 5, "index": 0, "note": "<what you learned>"}
Rules: prefer snapshot before click/type (use real selectors from snapshot). click_text clicks by visible words when selectors repeat. Clicks auto-follow _blank links into new tabs (check tabs). reads console for JS errors after key actions. network shows failed requests (check after page loads); perf gives load timing. device "pixel-8" (or iphone-15-pro) sees the site as a phone, rotate checks orientation, viewport resizes; throttle "offline"/"slow-3g" tests degraded networks. waits for slow pages. Use screenshot to verify visually. End with {"action":"done","note":"<verdict PASS/FAIL + summary>"}.
"""

TOOLS_HELP = "goto(url) snapshot click(selector) click_text(words) type(selector,text) press(key) scroll(pixels) wait(seconds) wait_for(js-cond) wait_idle js(expr) back reload dom console network perf device(name, no-arg lists presets) viewport(WxH) rotate throttle(preset) theme(mode) tabs tab(index) screenshot assert(needle) done"


def _chat(base_url: str, api_key: str, model: str, messages: list[dict[str, str]]) -> str:
    resp = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": messages, "temperature": 0.2},
        timeout=90,
    )
    resp.raise_for_status()
    return str(resp.json()["choices"][0]["message"]["content"])


def _exec(browser: HeadlessBrowser, call: dict[str, Any]) -> dict[str, Any]:
    # Same verbs as scripts (do_step normalizes agent-style text/WxH args), then delegate.
    action = str(call.get("action", "snapshot")).lower()
    if action == "done":
        return {"done": True, "note": str(call.get("note", ""))}
    if action == "screenshot":
        result = {"ok": True, "snapshot": browser.snapshot()}
        shot = browser.screenshot()
        result["screenshot_path"] = shot["path"]
        return result
    if action == "dom":
        result = browser.dom()
        result["html"] = result["html"][:2000]  # keep context small for the LLM
    else:
        result = do_step(browser, {**call, "action": action})  # do_step owns normalize()
    # Every step ends with a fresh screenshot so the agent "sees" the page.
    try:
        shot = browser.screenshot()
        result["screenshot_path"] = shot["path"]
    except Exception:
        pass
    return result


def run_agent(
    goal: str,
    start_url: str,
    max_steps: int = 15,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    headless: bool = True,
) -> dict[str, Any]:
    """Run the LLM-driven test loop. Returns transcript + verdict."""
    api_key = api_key or os.getenv("OPENAI_API_KEY", "")
    base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set (see .env.example). Use `shell` mode for manual/agent-piped testing.")
    browser = HeadlessBrowser(headless=headless)
    browser.start(start_url)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + f"Tools: {TOOLS_HELP}"},
        {"role": "user", "content": f"Goal: {goal}\nStart URL: {start_url}\nCurrent state: {json.dumps(browser.snapshot())}"},
    ]
    transcript: list[dict[str, Any]] = []
    verdict = "INCOMPLETE"
    for step in range(1, max_steps + 1):
        raw = _chat(base_url, api_key, model, messages)
        try:
            call: dict[str, Any] = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        except Exception:
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": "Reply with ONE JSON tool call only."})
            continue
        if call.get("action") == "done":
            verdict = str(call.get("note", "DONE"))
            transcript.append({"step": step, "call": call, "result": {"done": True}})
            break
        try:
            result = _exec(browser, call)
        except Exception as exc:  # surface failures to the LLM so it can retry
            result = {"ok": False, "error": short_error(exc)}
        transcript.append({"step": step, "call": call, "result": {k: v for k, v in result.items() if k != "png_base64"}})
        messages.append({"role": "assistant", "content": json.dumps(call)})
        messages.append({"role": "user", "content": f"Step {step} result: {json.dumps(result)[:3000]}"})
    browser.quit()
    return {"ok": True, "verdict": verdict, "steps": len(transcript), "transcript": transcript}
