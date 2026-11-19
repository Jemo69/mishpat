"""One action language for shell, helpful, run, record, and the agent loop.

Every command speaks the same verbs so agents never re-learn per mode:
goto snapshot click click_text type press scroll wait wait_for wait_idle
back forward reload dom console network perf tabs tab js checkpoint restore
screenshot assert device viewport rotate throttle theme
"""
from __future__ import annotations

from typing import Any

from browser import HeadlessBrowser

ACTIONS = (
    "goto snapshot click click_text type press scroll wait wait_for wait_idle "
    "back forward reload dom console network perf tabs tab js checkpoint restore screenshot assert "
    "device viewport rotate throttle theme"
)


def normalize(s: dict[str, Any]) -> dict[str, Any]:
    """One arg language for scripts + agent: accept text/WxH or explicit keys.

    Scripts use explicit keys (device/preset/mode/width/height); the agent
    loop favors a single `text` field and "WxH" viewports. Both land here."""
    s = dict(s)
    a = s.get("action", "")
    t = s.get("text", "")
    s.setdefault("text", "")
    if a == "goto":
        s.setdefault("url", t)
    elif a == "press":
        s.setdefault("key", t or "ENTER")
    elif a == "device":
        s.setdefault("device", s.get("name") or t or None)
    elif a == "viewport" and "width" not in s and t:
        parts = str(t).lower().replace("x", " ").split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            s["width"], s["height"] = parts
    elif a == "throttle":
        s.setdefault("preset", t or "online")
    elif a == "theme":
        s.setdefault("mode", t or "dark")
    elif a == "js":
        s.setdefault("expression", t or "document.readyState")
    elif a == "wait_for":
        s.setdefault("condition", "true")
    elif a == "tab":
        s.setdefault("index", 0)
    return s

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
    s = normalize(s)
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
    if a == "device":
        return b.emulate(s.get("device"), s.get("width"), s.get("height"))
    if a == "viewport":
        return b.viewport(int(s["width"]), int(s["height"]))
    if a == "rotate":
        return b.rotate()
    if a == "network":
        return b.network(limit=int(s.get("limit", 30)))
    if a == "perf":
        return b.perf()
    if a == "throttle":
        return b.throttle(str(s.get("preset", s.get("text", "online"))))
    if a == "theme":
        return b.theme(str(s.get("mode", s.get("text", "dark"))))
    raise ValueError(f"unknown action: {a} (valid: {ACTIONS})")
