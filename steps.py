"""One action language for shell, helpful, run, record, and the agent loop.

Every command speaks the same verbs so agents never re-learn per mode:
goto snapshot click click_text type press scroll wait back forward reload
dom console tabs tab screenshot assert
"""
from __future__ import annotations

from typing import Any

from browser import HeadlessBrowser

ACTIONS = (
    "goto snapshot click click_text type press scroll wait wait_for wait_idle "
    "back forward reload dom console tabs tab js checkpoint restore screenshot assert"
)

# Signatures that the chromedriver connection itself is gone (crash/OOM-kill).
# When these appear, the session is unrecoverable: abort, don't keep feeding stdin.
DRIVER_DEAD_HINTS = (
    "failed to establish a new connection",
    "connection refused",
    "max retries exceeded",
    "tried to run command without establishing a connection",
    "no such session",
    "session deleted",
    "session not created",
    "chrome not reachable",
    "invalid session id",
)

DRIVER_LOST = "DRIVER_LOST — the browser/driver died mid-run. Aborting."


def short_error(exc: Exception) -> str:
    """One-line error an agent can act on (no stack traces)."""
    msg = str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
    hint = ""
    if "StaleElement" in type(exc).__name__:
        hint = " (page re-rendered mid-action — just retry the command)"
    return f"{type(exc).__name__}: {msg}{hint}"


def is_driver_dead(exc: Exception) -> bool:
    """True when the driver connection itself is gone (not an app error)."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(h in text for h in DRIVER_DEAD_HINTS)


def do_step(b: HeadlessBrowser, s: dict[str, Any]) -> dict[str, Any]:
    """Execute one action dict. Raises LookupError/RuntimeError on failure."""
    a = s.get("action", "")
    if a == "goto":
        return b.goto(s["url"])
    if a == "click":
        return b.click(s["selector"])
    if a == "click_text":
        return b.click_text(s["text"])
    if a == "type":
        return b.type_text(s["selector"], s.get("text", ""))
    if a == "press":
        return b.press(s.get("key", "ENTER"))
    if a == "scroll":
        return b.scroll(int(s.get("pixels", 800)))
    if a == "wait":
        b.sleep(float(s.get("seconds", 2)))  # active use: reaper must not kill a scripted wait
        return {"ok": True, "waited": s.get("seconds", 2)}
    if a == "wait_for":
        return b.wait_for(s["condition"], float(s.get("timeout", 15)))
    if a == "wait_idle":
        return b.wait_idle(float(s.get("timeout", 20)))
    if a == "js":
        return b.js(s["expression"])
    if a == "checkpoint":
        return b.checkpoint(s["path"])
    if a == "restore":
        return b.restore(s["path"])
    if a == "back":
        return b.back()
    if a == "forward":
        return b.forward()
    if a == "reload":
        return b.reload()
    if a == "dom":
        return b.dom(limit=int(s.get("limit", 8000)))
    if a == "console":
        return b.console(limit=int(s.get("limit", 20)))
    if a == "tabs":
        return b.tabs()
    if a == "tab":
        return b.switch_tab(int(s["index"]))
    if a == "screenshot":
        return b.screenshot(s.get("name"))
    if a == "assert":
        r = b.assert_text(s["text"])
        if not r["ok"]:
            raise AssertionError(f"text not found: {s['text']!r}")
        return r
    if a == "snapshot":
        return b.snapshot()
    raise ValueError(f"unknown action: {a} (valid: {ACTIONS})")
