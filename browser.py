"""Headless Selenium wrapper. One class, one job: drive Chromium without a visible window."""
from __future__ import annotations

import base64
import functools
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Concatenate, ParamSpec, TypeVar

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

def short_exc(exc: Exception) -> str:
    """One-line error, no stack trace."""
    msg = str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
    return f"{type(exc).__name__}: {msg}"


SNAPSHOT_JS = """
const els = Array.from(document.querySelectorAll(
  'a, button, input, select, textarea, [role="button"], [role="link"], [onclick]'
));
return els.slice(0, 80).map((el, i) => {
  const r = el.getBoundingClientRect();
  let sel = el.tagName.toLowerCase();
  if (el.id) sel += '#' + el.id;
  else if (el.name) sel += '[name="' + el.name + '"]';
  else if (el.className && typeof el.className === 'string' && el.className.trim())
    sel += '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.');
  else if (el.getAttribute('href')) sel += '[href="' + el.getAttribute('href').slice(0, 60) + '"]';
  else if (el.type) sel += '[type="' + el.type + '"]';
  const text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').slice(0, 80).replace(/\\s+/g, ' ').trim();
  return {ref: i, tag: el.tagName.toLowerCase(), text, selector: sel,
          x: Math.round(r.x), y: Math.round(r.y),
          visible: r.width > 0 && r.height > 0};
});
"""


_S = TypeVar("_S", bound="HeadlessBrowser")
_P = ParamSpec("_P")
_R = TypeVar("_R")

# Mobile presets: CSS viewport + DPR + UA + touch. The agent "sees" the site
# as a phone by screenshotting under one of these (device <name>).
DEVICES: dict[str, dict[str, Any]] = {
    "iphone-15-pro": {
        "label": "iPhone 15 Pro", "width": 393, "height": 852, "dpr": 3.0,
        "mobile": True, "touch": True,
        "ua": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 "
               "Mobile/15E148 Safari/604.1"),
    },
    "iphone-se": {
        "label": "iPhone SE", "width": 375, "height": 667, "dpr": 2.0,
        "mobile": True, "touch": True,
        "ua": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 "
               "Mobile/15E148 Safari/604.1"),
    },
    "pixel-8": {
        "label": "Pixel 8", "width": 412, "height": 915, "dpr": 2.625,
        "mobile": True, "touch": True,
        "ua": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"),
    },
    "ipad-air": {
        "label": "iPad Air", "width": 820, "height": 1180, "dpr": 2.0,
        "mobile": True, "touch": True,
        "ua": ("Mozilla/5.0 (iPad; CPU OS 17_2 like Mac OS X) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 "
               "Mobile/15E148 Safari/604.1"),
    },
    # Desktop presets carry an explicit UA too: switching back from a phone
    # must overwrite the mobile UA override (CDP has no "clear", only "set").
    "desktop": {
        "label": "Desktop", "width": 1280, "height": 900, "dpr": 1.0,
        "mobile": False, "touch": False,
        "ua": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    },
    "desktop-hd": {
        "label": "Desktop HD", "width": 1920, "height": 1080, "dpr": 1.0,
        "mobile": False, "touch": False,
        "ua": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    },
}

# CDP Network.emulateNetworkConditions presets (throughput in bytes/sec).
# "online" resets to unthrottled. Values mirror DevTools' mobile presets.
NETWORK_PRESETS: dict[str, dict[str, Any]] = {
    "offline": {"offline": True, "latency": 0, "download": 0, "upload": 0},
    "slow-3g": {"offline": False, "latency": 2000, "download": 50 * 1024, "upload": 50 * 1024},
    "fast-3g": {"offline": False, "latency": 150, "download": 200 * 1024, "upload": 96 * 1024},
    "4g": {"offline": False, "latency": 20, "download": 512 * 1024, "upload": 384 * 1024},
    "online": {"offline": False, "latency": 0, "download": -1, "upload": -1},
}


def _held(fn: Callable[Concatenate[_S, _P], _R]) -> Callable[Concatenate[_S, _P], _R]:
    """Run a whole driver operation under the idle-reaper lock.

    The reaper may only close Chromium *between* calls: without this, a call
    that outlives idle_timeout (slow page load, short configured timeout) loses
    its session mid-operation. Touching on completion keeps the idle clock
    honest for ops of any length."""
    @functools.wraps(fn)
    def wrapper(head: _S, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        with head._lock:
            try:
                return fn(head, *args, **kwargs)
            finally:
                head._touch()  # we just used it, however long that took
    return wrapper


class HeadlessBrowser:
    """Owns a single headless Chromium session."""

    def __init__(
        self,
        headless: bool = True,
        width: int = 1280,
        height: int = 900,
        shots_dir: str | Path = "shots",
        timeout: int = 15,
        quiet: bool = False,
        driver_port: int = 0,
        idle_timeout: float = 60.0,
        device: str | None = None,
    ) -> None:
        self.headless = headless
        # A device preset overrides width/height with its CSS viewport.
        if device is not None and device not in DEVICES:
            raise ValueError(f"unknown device: {device!r} (try: {', '.join(DEVICES)})")
        name = device or "desktop"
        preset = DEVICES[name]
        self.width = preset["width"] if device else width
        self.height = preset["height"] if device else height
        # Live emulation state (CDP override); viewport()/rotate() mutate this.
        self._emulation: dict[str, Any] = {
            "device": name,
            "width": self.width,
            "height": self.height,
            "dpr": preset["dpr"],
            "mobile": preset["mobile"],
            "touch": preset["touch"],
            "ua": preset["ua"],
        }
        # DevTools state that must survive an idle-reaper relaunch (C2).
        self._throttle = "online"
        self._theme: str = "none"
        self.shots_dir = Path(shots_dir)
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.quiet = quiet  # paths only, no base64 noise
        self.driver_port = driver_port  # 0 = random; set to isolate parallel runs
        self.idle_timeout = idle_timeout  # 0 = never auto-quit; >0 = quit driver after N s idle
        self.driver: webdriver.Chrome | None = None
        self._shot_count = 0
        self.tag = time.strftime("%m%d-%H%M%S")  # session prefix: shots never collide
        self._last_elements: list[dict[str, Any]] = []
        self._last_used = time.monotonic()
        self._relaunches = 0  # silent session replacements (idle reap / stale start)
        self._reaper: threading.Thread | None = None
        self._reaper_stop: threading.Event | None = None
        self._lock = threading.RLock()  # serializes reaper-close vs. any driver
        # operation (RLock: close_if_idle already holds it when it calls quit())

    def _touch(self) -> None:
        """Mark browser as used now (resets idle timer)."""
        self._last_used = time.monotonic()

    def idle_seconds(self) -> float:
        """Seconds since last browser use."""
        return time.monotonic() - self._last_used

    def _idle_expired(self) -> bool:
        return (
            self.driver is not None
            and self.idle_timeout > 0
            and self.idle_seconds() >= self.idle_timeout
        )

    def close_if_idle(self) -> dict[str, Any]:
        """Quit Chromium if idle past timeout. Reaper thread only; _require/start
        handle expiry inline."""
        with self._lock:  # check+close must be atomic vs. a concurrent start/_require
            if self._idle_expired():
                idle_for = round(self.idle_seconds(), 1)
                self.quit()
                # Count the silent reset where it HAPPENS: a post-reap relaunch
                # lands in _require's driver-None branch, which must not count
                # first-use launches (otherwise relaunches:1 on a fresh session).
                self._relaunches += 1
                return {"ok": True, "auto_closed": True, "idle_for_s": idle_for}
            return {"ok": True, "auto_closed": False, "idle_for_s": round(self.idle_seconds(), 1)}

    def idle_status(self) -> dict[str, Any]:
        """Inspect idle state without closing (MCP debugging)."""
        with self._lock:  # consistent view while the reaper is acting
            return {
                "ok": True,
                "alive": self.driver is not None,
                "idle_for_s": round(self.idle_seconds(), 1),
                "idle_timeout_s": self.idle_timeout,
                "will_autoclose": self._idle_expired(),
                "relaunches": self._relaunches,  # >0 means the page was silently reset
            }

    @_held
    def sleep(self, seconds: float) -> None:
        """Explicit wait = active use: holds the reaper lock, refreshes the clock."""
        time.sleep(seconds)

    def _start_reaper(self) -> None:
        """Background daemon: quit Chromium after `idle_timeout` s with no calls.

        Without this, one MCP server per agent = orphaned `chromium --headless`
        processes piling up (56 seen). Next tool call transparently relaunches."""
        if self.idle_timeout <= 0:
            return
        if self._reaper is not None and self._reaper.is_alive():
            return
        self._reaper_stop = threading.Event()
        stop = self._reaper_stop

        def _loop() -> None:
            # tick scales with the deadline so small idle_timeout close on time
            # (0–5s late on a fixed 5s tick reads as a broken promise in --help)
            while not stop.wait(min(5.0, max(0.25, self.idle_timeout / 4))):
                try:
                    self.close_if_idle()
                except Exception:
                    pass  # never kill the host process from the reaper
                if stop.is_set():
                    break

        self._reaper = threading.Thread(target=_loop, name="mishpat-idle-reaper", daemon=True)
        self._reaper.start()

    def _stop_reaper(self) -> threading.Thread | None:
        """Arm the stop event; return the thread so quit() can join it *outside*
        the lock (the reaper may itself be queued on that lock)."""
        if self._reaper_stop is not None:
            self._reaper_stop.set()
            self._reaper_stop = None
        reaper, self._reaper = self._reaper, None
        return reaper

    def start(self, url: str | None = None) -> dict[str, Any]:
        """Launch Chromium (headless by default). Optionally navigate."""
        # Whole body under the lock: decide+launch+publish atomically, so the
        # reaper can never observe a half-built session (or return a driver it
        # is about to quit) and two calls can't launch twin Chromes.
        with self._lock:
            if self.driver is not None:
                if self._idle_expired():
                    self.quit()  # stale idle session: drop it, relaunch below
                    self._relaunches += 1
                else:
                    self._touch()
                    return {"ok": True, "reused": True, "url": self.driver.current_url}
            opts = Options()
            if self.headless:
                opts.add_argument("--headless=new")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-gpu")
            opts.add_argument(f"--window-size={self.width},{self.height}")
            opts.add_argument("--log-level=3")
            # Never touch the desktop notification daemon: on WSLg each Chrome launch
            # can D-Bus-activate xfce4-notifyd, which pops "Unable to start notification
            # daemon" on the Windows side. Headless browsing needs no notifications.
            opts.add_argument("--disable-notifications")
            # browser log feeds console(); performance log feeds network().
            opts.set_capability("goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"})
            opts.add_experimental_option("perfLoggingPrefs", {"enableNetwork": True})
            # Steering away from automation banners breaks fewer sites than you'd think.
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            env = dict(os.environ)
            env.pop("DBUS_SESSION_BUS_ADDRESS", None)  # keep Chrome off the session bus
            # --disable-dev-shm-usage (above) + isolated ports: the two classic fixes for
            # headless-Chrome crashes and driver collisions under parallel runs.
            service = Service(log_output=os.devnull, env=env, port=self.driver_port)
            self.driver = webdriver.Chrome(options=opts, service=service)
            self.driver.set_page_load_timeout(self.timeout)
            self.driver.implicitly_wait(3)
            self._apply_emulation()  # device preset before first navigation
            self._apply_net_state()  # reapply stored throttle/theme after relaunch
            self._touch()
            self._start_reaper()
        if url:
            return self.goto(url)
        return {"ok": True, "url": None, "headless": self.headless}

    def _require(self) -> webdriver.Chrome:
        with self._lock:
            if self.driver is None:
                self.start()
            elif self._idle_expired():
                self.quit()
                self.start()
                self._relaunches += 1
            self._touch()
            d = self.driver
        assert d is not None  # lock held through the handoff; reaper can't close in between
        return d

    @_held
    def goto(self, url: str) -> dict[str, Any]:
        """Navigate to a URL (adds https:// if scheme missing)."""
        if not url.startswith(("http://", "https://", "file://")):
            url = "https://" + url
        d = self._require()
        d.get(url)
        return {"ok": True, "url": d.current_url, "title": d.title}

    @_held
    def snapshot(self) -> dict[str, Any]:
        """URL + title + interactive elements ([N] refs are re-clickable via click)."""
        d = self._require()
        elements: list[dict[str, Any]] = d.execute_script(SNAPSHOT_JS)
        self._last_elements = elements
        return {
            "ok": True,
            "url": d.current_url,
            "title": d.title,
            "viewport": {"width": self.width, "height": self.height},
            "elements": elements,
        }

    # ---- mobile emulation + devtools (CDP) ----

    def _apply_emulation(self) -> None:
        """Push self._emulation to Chromium via CDP. Call with the lock held."""
        d = self.driver
        if d is None:
            return
        e = self._emulation
        try:
            d.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                "width": e["width"], "height": e["height"],
                "deviceScaleFactor": e["dpr"], "mobile": e["mobile"],
            })
        except Exception:
            pass  # older drivers: window-size arg still applies
        try:
            d.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": e["touch"]})
        except Exception:
            pass
        # Always set (never skip): every preset carries a UA so switching
        # back to desktop overwrites a stale mobile UA (C1).
        try:
            d.execute_cdp_cmd("Emulation.setUserAgentOverride", {"userAgent": e["ua"]})
        except Exception:
            pass

    def _apply_net_state(self) -> None:
        """Reapply stored throttle/theme to a fresh driver. Lock held. Best-effort:
        a relaunch must never fail because a CDP nicety did."""
        d = self.driver
        if d is None:
            return
        p = NETWORK_PRESETS.get(self._throttle, NETWORK_PRESETS["online"])
        try:
            d.execute_cdp_cmd("Network.enable", {})
            d.execute_cdp_cmd("Network.emulateNetworkConditions", {
                "offline": p["offline"], "latency": p["latency"],
                "downloadThroughput": p["download"], "uploadThroughput": p["upload"],
            })
        except Exception:
            pass
        if self._theme != "none":
            try:
                d.execute_cdp_cmd("Emulation.setEmulatedMedia", {
                    "features": [{"name": "prefers-color-scheme", "value": self._theme}]})
            except Exception:
                pass

    @_held
    def emulate(
        self,
        device: str | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> dict[str, Any]:
        """See the site as a mobile device: viewport + DPR + touch + mobile UA.

        emulate("pixel-8") switches preset; width/height override just the dims
        (keeps the current preset's touch/UA). emulate("desktop") resets.
        Bare emulate() lists the presets (no browser launch needed)."""
        if device is None and width is None and height is None:
            return {
                "ok": True,
                "current": dict(self._emulation),
                "devices": {
                    name: {k: p[k] for k in ("label", "width", "height", "dpr", "mobile", "touch")}
                    for name, p in DEVICES.items()
                },
            }
        d = self._require()
        if device is not None:
            if device not in DEVICES:
                raise ValueError(f"unknown device: {device!r} (try: {', '.join(DEVICES)})")
            p = DEVICES[device]
            self._emulation.update({
                "device": device, "width": p["width"], "height": p["height"],
                "dpr": p["dpr"], "mobile": p["mobile"],
                "touch": p["touch"], "ua": p["ua"],
            })
        if width is not None:
            self._emulation["width"] = int(width)
        if height is not None:
            self._emulation["height"] = int(height)
        self._emulation["device"] = device or self._emulation["device"]
        self.width, self.height = self._emulation["width"], self._emulation["height"]
        try:
            # Window resize first: it can clear a CDP metrics override, so the
            # override below must be the last writer.
            d.set_window_size(self.width, self.height)
        except Exception:
            pass
        self._apply_emulation()
        return {"ok": True, "emulation": dict(self._emulation), "url": d.current_url}

    @_held
    def viewport(self, width: int, height: int) -> dict[str, Any]:
        """Resize the CSS viewport (keeps current device's touch/UA)."""
        return self.emulate(width=int(width), height=int(height))

    @_held
    def rotate(self) -> dict[str, Any]:
        """Swap portrait/landscape (mobile orientation check)."""
        d = self._require()
        w, h = d.execute_script("return [window.innerWidth, window.innerHeight];")
        return self.emulate(width=int(h), height=int(w))

    @_held
    def network(self, limit: int = 30) -> dict[str, Any]:
        """DevTools Network in the terminal: recent requests with status.

        Failures (4xx/5xx/loadingFailed) surface first so the agent spots
        broken APIs without opening DevTools."""
        d = self._require()
        reqs: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        try:
            logs = d.get_log("performance")
        except Exception as exc:
            logs = []
            log_error = f"performance log unavailable: {exc}"
        else:
            log_error = ""
        import json as _json
        for entry in logs:
            try:
                msg = _json.loads(entry.get("message", "{}")).get("message", {})
            except Exception:
                continue
            method, p = msg.get("method", ""), msg.get("params", {})
            if method == "Network.requestWillBeSent":
                rid = p.get("requestId", "")
                url = p.get("request", {}).get("url", "")[:160]
                if rid and rid not in reqs:
                    reqs[rid] = {"url": url,
                                 "status": None, "mime": "", "failed": False, "error": ""}
                    order.append(rid)
                elif rid and url:
                    # Redirect hop reuses the requestId: follow it to the final URL.
                    reqs[rid]["url"] = url
            elif method == "Network.responseReceived":
                rid = p.get("requestId", "")
                if rid in reqs:
                    resp = p.get("response", {})
                    reqs[rid]["status"] = resp.get("status")
                    reqs[rid]["mime"] = str(resp.get("mimeType", ""))[:60]
                    if isinstance(resp.get("status"), int) and resp["status"] >= 400:
                        reqs[rid]["failed"] = True
            elif method == "Network.loadingFailed":
                rid = p.get("requestId", "")
                if rid in reqs:
                    reqs[rid]["failed"] = True
                    reqs[rid]["error"] = str(p.get("errorText", ""))[:120]
        # data: noise out. Failures first (the agent's signal), then most recent.
        items = [reqs[rid] for rid in order
                 if reqs[rid]["url"] and not reqs[rid]["url"].startswith("data:")]
        failed = [r for r in items if r["failed"]]
        ok_items = [r for r in items if not r["failed"]]
        return {
            "ok": True, "url": d.current_url,
            "total": len(items), "failed_count": len(failed),
            "failed": failed[:limit],
            "recent": (failed + ok_items[-max(0, limit - len(failed)):])[:limit],
            **({"log_error": log_error} if log_error else {}),
        }

    @_held
    def perf(self) -> dict[str, Any]:
        """DevTools Performance in one dict: navigation + paint timing."""
        d = self._require()
        timing = d.execute_script(
            "const n = performance.getEntriesByType('navigation')[0] || {};"
            "const paint = Object.fromEntries(performance.getEntriesByType('paint')"
            ".map(e => [e.name, Math.round(e.startTime)]));"
            "return {"
            "  domContentLoaded: Math.round(n.domContentLoadedEventEnd || 0),"
            "  load: Math.round(n.loadEventEnd || 0),"
            "  domInteractive: Math.round(n.domInteractive || 0),"
            "  transferBytes: n.transferSize || 0,"
            "  resources: performance.getEntriesByType('resource').length,"
            "  firstPaint: paint['first-paint'] ?? null,"
            "  firstContentfulPaint: paint['first-contentful-paint'] ?? null};"
        )
        return {"ok": True, "url": d.current_url, **timing}

    @_held
    def throttle(self, preset: str = "online") -> dict[str, Any]:
        """DevTools throttling: offline | slow-3g | fast-3g | 4g | online.

        throttle("offline") then goto proves the app's offline story."""
        d = self._require()
        if preset not in NETWORK_PRESETS:
            raise ValueError(f"unknown preset: {preset!r} (try: {', '.join(NETWORK_PRESETS)})")
        p = NETWORK_PRESETS[preset]
        try:
            d.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass
        try:
            d.execute_cdp_cmd("Network.emulateNetworkConditions", {
                "offline": p["offline"], "latency": p["latency"],
                "downloadThroughput": p["download"], "uploadThroughput": p["upload"],
            })
        except Exception as exc:
            raise RuntimeError(f"throttle {preset!r} failed: {short_exc(exc)}")
        self._throttle = preset  # relaunches reapply it (C2)
        return {"ok": True, "throttle": preset, **p}

    @_held
    def theme(self, mode: str = "dark") -> dict[str, Any]:
        """Emulate prefers-color-scheme: dark | light | none (clears override)."""
        d = self._require()
        if mode not in ("dark", "light", "none"):
            raise ValueError(f"unknown theme: {mode!r} (try: dark | light | none)")
        features = [] if mode == "none" else [{"name": "prefers-color-scheme", "value": mode}]
        try:
            d.execute_cdp_cmd("Emulation.setEmulatedMedia", {"features": features})
        except Exception as exc:
            raise RuntimeError(f"theme {mode!r} failed: {short_exc(exc)}")
        self._theme = mode  # relaunches reapply it (C2)
        return {"ok": True, "theme": mode}

    def _retry_once(self, fn: Callable[[], Any]) -> Any:
        """Run fn; on a stale-element race (SPA re-rendered mid-action), retry once."""
        try:
            return fn()
        except StaleElementReferenceException:
            return fn()

    def _follow_new_tab(self, d: webdriver.Chrome, before: list[str]) -> dict[str, Any]:
        """If the click opened a tab (target=_blank), follow it. No-op otherwise."""
        deadline = time.time() + 2.0
        while time.time() < deadline:
            new = [h for h in d.window_handles if h not in before]
            if new:
                d.switch_to.window(new[0])
                return {"followed_new_tab": True, "tabs": len(d.window_handles), "url": d.current_url}
            time.sleep(0.2)
        return {"followed_new_tab": False, "tabs": len(d.window_handles), "url": d.current_url}

    def _resolve_index(self, selector: str) -> str:
        """Accept a snapshot ref like [12]: resolve to its selector. Guaranteed re-clickable."""
        import re as _re

        m = _re.fullmatch(r"\[(\d+)\]", selector.strip())
        if not m:
            return selector
        if not self._last_elements:
            raise LookupError("no snapshot taken yet — run snapshot first, then click [N]")
        i = int(m.group(1))
        if not 0 <= i < len(self._last_elements):
            raise LookupError(f"snapshot ref [{i}] out of range (0..{len(self._last_elements) - 1})")
        return self._last_elements[i]["selector"]

    @_held
    def click(self, selector: str) -> dict[str, Any]:
        """Click first element matching a CSS selector (or a snapshot [N] ref)."""
        d = self._require()
        selector = self._resolve_index(selector)
        before = list(d.window_handles)

        def _do() -> None:
            el = WebDriverWait(d, self.timeout).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            el.click()

        try:
            self._retry_once(_do)
        except TimeoutException:
            raise LookupError(f"nothing clickable matches: {selector!r}")
        out: dict[str, Any] = {"ok": True, "clicked": selector}
        out.update(self._follow_new_tab(d, before))
        return out

    @_held
    def click_text(self, text: str) -> dict[str, Any]:
        """Click first visible button/link/tab whose text contains `text`. Follows new tabs."""
        d = self._require()
        before = list(d.window_handles)
        lit = f'"{text}"' if "'" not in text else f"'{text}'"
        xp = (
            f"//*[self::a or self::button or self::div[@role='tab'] or self::span]"
            f"[contains(normalize-space(), {lit})]"
        )

        def _do() -> None:
            els = [e for e in d.find_elements(By.XPATH, xp) if e.is_displayed()]
            if not els:
                raise LookupError(f"no visible control containing text: {text!r}")
            els[0].click()

        self._retry_once(_do)
        out = {"ok": True, "clicked_text": text}
        out.update(self._follow_new_tab(d, before))
        return out

    @_held
    def type_text(self, selector: str, text: str, submit: bool = False) -> dict[str, Any]:
        """Clear + fill a VISIBLE input/textarea. Hidden matches fail loudly (closed dialog?)."""
        d = self._require()
        try:
            el = self._retry_once(
                lambda: WebDriverWait(d, self.timeout).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, selector))
                )
            )
        except TimeoutException:
            raise LookupError(
                f"no VISIBLE input matches: {selector!r} (missing, or hidden in a closed dialog)"
            )
        el.clear()
        el.send_keys(text)
        if submit:
            el.send_keys(Keys.ENTER)
        return {"ok": True, "typed": selector, "chars": len(text)}

    @_held
    def press(self, key: str = "ENTER") -> dict[str, Any]:
        """Press a key on the focused element (ENTER, TAB, ESCAPE, ...)."""
        d = self._require()
        el = d.switch_to.active_element
        el.send_keys(getattr(Keys, key.upper(), Keys.ENTER))
        return {"ok": True, "pressed": key, "url": d.current_url}

    @_held
    def scroll(self, pixels: int = 800) -> dict[str, Any]:
        """Scroll vertically by pixels (negative = up)."""
        d = self._require()
        d.execute_script("window.scrollBy(0, arguments[0]);", pixels)
        y: int = d.execute_script("return window.scrollY;")
        return {"ok": True, "scrollY": y}

    @_held
    def back(self) -> dict[str, Any]:
        d = self._require()
        d.back()
        return {"ok": True, "url": d.current_url}

    @_held
    def forward(self) -> dict[str, Any]:
        d = self._require()
        d.forward()
        return {"ok": True, "url": d.current_url}

    @_held
    def reload(self) -> dict[str, Any]:
        d = self._require()
        d.refresh()
        return {"ok": True, "url": d.current_url}

    @_held
    def dom(self, limit: int = 8000) -> dict[str, Any]:
        """Full HTML, truncated to `limit` chars."""
        d = self._require()
        html: str = d.execute_script("return document.documentElement.outerHTML;")
        return {"ok": True, "url": d.current_url, "length": len(html), "html": html[:limit]}

    @_held
    def assert_text(self, text: str) -> dict[str, Any]:
        """Check visible text contains `text`."""
        d = self._require()
        body = d.find_element(By.TAG_NAME, "body").text
        return {"ok": text in body, "found": text in body, "needle": text}

    @_held
    def tabs(self) -> dict[str, Any]:
        """List open tabs with URLs. Jump with switch_tab."""
        d = self._require()
        cur = d.current_window_handle
        info: list[dict[str, Any]] = []
        try:
            for i, h in enumerate(d.window_handles):
                d.switch_to.window(h)
                info.append({"index": i, "url": d.current_url, "title": d.title})
        finally:
            d.switch_to.window(cur)
        return {"ok": True, "current": d.window_handles.index(cur), "tabs": info}

    @_held
    def switch_tab(self, index: int) -> dict[str, Any]:
        """Jump to tab by index (see tabs)."""
        d = self._require()
        handles = d.window_handles
        if not 0 <= index < len(handles):
            raise LookupError(f"tab {index} out of range (0..{len(handles) - 1})")
        d.switch_to.window(handles[index])
        return {"ok": True, "tab": index, "url": d.current_url, "title": d.title}

    @_held
    def console(self, limit: int = 20) -> dict[str, Any]:
        """Recent browser console entries: JS errors, failed requests. Debug without leaving the CLI."""
        d = self._require()
        try:
            entries = d.get_log("browser")[-limit:]
        except Exception as exc:
            return {"ok": False, "error": f"console log unavailable: {exc}"}
        return {
            "ok": True,
            "entries": [{"level": e.get("level"), "message": e.get("message", "")[:500]} for e in entries],
        }

    @_held
    def js(self, expression: str) -> dict[str, Any]:
        """Evaluate a JS expression, return JSON-able result. Sets auth, reads state, polls."""
        d = self._require()
        try:
            value = d.execute_script(f"return ({expression})")
        except Exception as exc:
            raise RuntimeError(f"js failed: {short_exc(exc)}")
        try:
            import json as _json

            _json.dumps(value)
            return {"ok": True, "value": value}
        except (TypeError, ValueError):
            return {"ok": True, "value": str(value)}

    @_held
    def wait_for(self, condition: str, timeout: float = 15) -> dict[str, Any]:
        """Poll a JS boolean until true. A broken expression fails fast (first eval raises)."""
        d = self._require()
        try:
            if d.execute_script(f"return !!({condition})"):
                return {"ok": True, "condition": condition}
        except Exception as exc:
            raise RuntimeError(f"bad wait_for expression {condition!r}: {short_exc(exc)}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if d.execute_script(f"return !!({condition})"):
                    return {"ok": True, "condition": condition}
            except Exception:
                pass  # transient mid-poll (navigation); keep polling
            time.sleep(0.5)
        raise TimeoutError(f"wait_for timed out after {timeout}s: {condition}")

    @_held
    def wait_idle(self, timeout: float = 20) -> dict[str, Any]:
        """Wait for hydration to settle: document ready + DOM quiet for ~1s.

        Call after goto before click/type on JS-heavy apps — guards against
        native-form-submit-before-hydration (credentials in URL) surprises."""
        d = self._require()
        deadline = time.time() + timeout
        last_count, quiet_since = -1, 0.0
        while time.time() < deadline:
            try:
                ready = d.execute_script("return document.readyState")
                count = len(d.find_elements(By.CSS_SELECTOR, "*"))
            except Exception:
                time.sleep(0.5)
                continue
            now = time.time()
            if ready == "complete" and count == last_count:
                if now - quiet_since >= 1.0:
                    return {"ok": True, "idle_after_s": round(now - (deadline - timeout), 1)}
            else:
                quiet_since = now
            last_count = count
            time.sleep(0.5)
        raise TimeoutError(f"page never idled within {timeout}s")

    @_held
    def checkpoint(self, path: str | Path) -> dict[str, Any]:
        """Save session state (URL, cookies, local+session storage) to a file.

        Restore it later to skip UI login: `shell --restore ck.json`."""
        d = self._require()
        state = {
            "url": d.current_url,
            "cookies": d.get_cookies(),
            "localStorage": d.execute_script("return Object.assign({}, window.localStorage);"),
            "sessionStorage": d.execute_script("return Object.assign({}, window.sessionStorage);"),
        }
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(__import__("json").dumps(state))
        return {"ok": True, "path": str(out)}

    @_held
    def restore(self, path: str | Path) -> dict[str, Any]:
        """Restore a checkpoint: cookies + storage back, land logged-in."""
        import json as _json

        d = self._require()
        state = _json.loads(Path(path).read_text())
        d.get(state["url"])  # domain must match before cookies can be set
        d.delete_all_cookies()
        for c in state.get("cookies", []):
            c.pop("sameSite", None)  # chromedriver rejects unrecognized values
            try:
                d.add_cookie(c)
            except Exception:
                pass
        for k, v in (state.get("localStorage") or {}).items():
            d.execute_script("window.localStorage.setItem(arguments[0], arguments[1]);", k, v)
        for k, v in (state.get("sessionStorage") or {}).items():
            d.execute_script("window.sessionStorage.setItem(arguments[0], arguments[1]);", k, v)
        d.get(state["url"])
        return {"ok": True, "url": d.current_url, "title": d.title}

    @_held
    def screenshot(self, name: str | None = None) -> dict[str, Any]:
        """Capture viewport PNG into shots dir. quiet=True returns path only (parseable stdout)."""
        d = self._require()
        self._shot_count += 1
        out = Path(name) if name else self.shots_dir / f"shot-{self.tag}-{self._shot_count:03d}.png"
        if not out.is_absolute():
            out = self.shots_dir / out.name if out.parent == Path(".") else out
        out.parent.mkdir(parents=True, exist_ok=True)
        d.save_screenshot(str(out))
        result: dict[str, Any] = {"ok": True, "path": str(out)}
        if not self.quiet:
            raw = base64.b64encode(out.read_bytes()).decode() if out.exists() else ""
            result["png_base64"] = raw[:200] + "…(truncated, read file)" if raw else ""
        return result

    def quit(self) -> dict[str, Any]:
        reaper: threading.Thread | None = None
        with self._lock:  # RLock: close_if_idle already holds it when it calls us
            reaper = self._stop_reaper()
            if self.driver is not None:
                try:
                    self.driver.quit()
                finally:
                    self.driver = None
        # Join only when this thread doesn't already own the lock (a nested quit
        # from start/_require would block the very reaper it wants to retire) and
        # never from the reaper itself. Skipped joins are safe: a stale reaper
        # re-checks expiry under the lock and exits on its already-set stop event.
        if (
            reaper is not None
            and reaper is not threading.current_thread()
            and not self._lock._is_owned()  # pyright: ignore[reportAttributeAccessIssue] — CPython RLock API, suggested by review
        ):
            reaper.join(timeout=2)
        return {"ok": True, "closed": True}
