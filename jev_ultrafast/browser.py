"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import math
import os
import random
import re
import secrets
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .coordination import OriginLock, agent_name

# One browser-harness daemon per agent name: parallel agents each get their own
# daemon, tab, and CDP event stream against the shared Chrome, so nobody drains
# another agent's events. The harness reads BU_NAME at import time, so the name
# is settled before those imports run.
AGENT_NAME = agent_name()
if os.environ.get("BU_NAME") != AGENT_NAME:
    os.environ["BU_NAME"] = AGENT_NAME

# Deliberately after the BU_NAME settlement above: the harness snapshots its
# name from the environment at import time.
from browser_harness.admin import daemon_alive, ensure_daemon  # noqa: E402
from browser_harness.helpers import cdp, drain_events  # noqa: E402

# Each process hides its snapshot cache under a fresh, non-enumerable window property.
# getOwnPropertyNames still lists hidden properties, so the key carries no recognizable
# prefix (chromedriver's cdc_ is caught exactly that way) — full enumeration finds only
# one anonymous-looking identifier that changes every run.
FAST_KEY = secrets.choice("abcdefghijk") + secrets.token_hex(8)
_KEY_JSON = json.dumps(FAST_KEY)

def _js(source):
    """Embed the per-process cache key into a page-side expression."""
    return source.replace("__JEV_KEY__", _KEY_JSON)

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = _js(Path(__file__).with_name("snapshot.js").read_text())
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

# Passive evidence collector: uncaught exceptions and unhandled rejections reach the
# window error listeners without Runtime.enable, whose console serialization pages
# can observe. Set JEV_RUNTIME_ERRORS=1 to restore deep debugger capture for debugging.
PAGE_ERROR_COLLECTOR = _js("""
(() => {
  const key=__JEV_KEY__;
  if (!window[key]) Object.defineProperty(window,key,
    {value:{},enumerable:false,configurable:true,writable:true});
  const store=window[key];
  if (store.page_errors) return;
  store.page_errors=[];
  window.addEventListener('error',e=>{
    if (store.page_errors.length>=200) return;
    const err=e.error;
    store.page_errors.push({kind:'exception',
      text:(err&&(err.stack||err.message))||e.message||'Error',url:e.filename||''});
  });
  window.addEventListener('unhandledrejection',e=>{
    if (store.page_errors.length>=200) return;
    const r=e.reason;
    store.page_errors.push({kind:'exception',text:(r&&(r.stack||r.message))||String(r),url:''});
  });
})()
""")

# Keep typing and clicks inside what a hand on a real keyboard produces.
TYPE_KEY_LIMIT = 160

# Per-session input state: where the cursor last rested and the emulated viewport.
# browser_operation receives only a session id, so the registry is the carrier.
_session_state = {}

def typing_plan(text):
    """('keys', text) when per-character trusted key events fit, else ('insert', text)."""
    if (text and os.environ.get("JEV_TYPE_MODE", "").strip().lower() != "insert"
            and len(text) <= TYPE_KEY_LIMIT and all(0x20 <= ord(char) <= 0x7E for char in text)):
        return "keys", text
    return "insert", text

def typing_gaps(text):
    """Inter-key delays: quick bursts inside a word, a longer pause starting the next one.
    The bands are disjoint so a page measuring keystroke rhythm sees a word-shaped cadence."""
    gaps = []
    for char in text[:-1]:
        if char == " ":
            gaps.append(random.uniform(0.035, 0.075))  # onset of a new word
        elif not (char.isalnum() and char.isascii()):
            gaps.append(random.uniform(0.018, 0.045))  # punctuation or symbol
        else:
            gaps.append(random.uniform(0.004, 0.012))  # inside a word
    return gaps

def char_key_params(char, press=True):
    """CDP keyDown (press=True) / keyUp parameters for one printable ASCII character.

    Insertion is driven by the text field on keyDown; a key event without text only
    presses the key and types nothing."""
    if char.isalpha() and char.isascii():
        params = {"key": char, "code": f"Key{char.upper()}",
                  "windowsVirtualKeyCode": ord(char.upper()),
                  "modifiers": 8 if char.isupper() else 0}
    elif char.isdigit():
        params = {"key": char, "code": f"Digit{char}", "windowsVirtualKeyCode": ord(char)}
    elif char == " ":
        params = {"key": " ", "code": "Space", "windowsVirtualKeyCode": 32}
    else:
        params = {"key": char}
    if press:
        params.update(text=char, unmodifiedText=char.lower())
    return params

def arrow_params(direction):
    """CDP parameters for one trusted ArrowDown/ArrowUp press."""
    return {"key": f"Arrow{direction}", "code": f"Arrow{direction}",
            "windowsVirtualKeyCode": 40 if direction == "Down" else 38}

def viewport_for_run():
    """A plausible desktop window size per run; JEV_VIEWPORT=WxH pins one for recordings."""
    spec = os.environ.get("JEV_VIEWPORT", "").strip().lower()
    if spec:
        try:
            width, height = (int(part) for part in spec.split("x"))
        except ValueError:
            raise ValueError("JEV_VIEWPORT must be WxH, e.g. 1280x800") from None
        if width < 400 or height < 400:
            raise ValueError("JEV_VIEWPORT must be at least 400x400")
        return width, height
    return random.choice(((1120, 780), (1280, 800), (1366, 768), (1440, 860),
                          (1536, 864), (1680, 947), (1920, 955)))

def scroll_point(session):
    """A wheel position that varies like a hand on the wheel, inside the live viewport."""
    width, height = _session_state.get(session, {}).get("viewport") or (1120, 780)
    return (random.randint(width // 5, width * 4 // 5),
            random.randint(height // 5, height * 7 // 10))

def wheel_plan(total_delta, rng=random):
    """A wheel flick: 4-6 notched ticks that land on the target distance and, when the
    flick is long, sometimes the small ease-back a hand does after overshooting."""
    sign = 1 if total_delta > 0 else -1
    remaining = abs(int(total_delta))
    if remaining <= 0:
        return []
    notches = []
    ticks = rng.randint(4, 6)
    for index in range(ticks):
        share = remaining / (ticks - index)
        size = max(28, min(round(share * rng.uniform(0.75, 1.25)), remaining))
        notches.append(sign * size)
        remaining -= size
    if remaining > 0:
        notches[-1] += sign * remaining
    if abs(int(total_delta)) >= 400 and rng.random() < 0.35:
        notches.append(-sign * rng.randint(30, 90))
    return notches

def path_points(x0, y0, x1, y1, rng=random):
    """Cursor waypoints to the target: a slight perpendicular bow, fast start,
    decelerating approach, hand tremor. The final point lands exactly on target."""
    dx, dy = x1 - x0, y1 - y0
    distance = math.hypot(dx, dy)
    if distance < 2:
        return []
    steps = 1 if distance < 40 else min(8, max(3, round(distance / 130)))
    bow = rng.uniform(-1, 1) * min(90, distance * 0.18)
    unit_x, unit_y = -dy / distance, dx / distance
    points = []
    for index in range(1, steps + 1):
        ease = 1 - (1 - index / steps) ** 2
        wobble = math.sin(math.pi * ease) * bow
        points.append((round(x0 + dx * ease + unit_x * wobble + rng.uniform(-2, 2)),
                       round(y0 + dy * ease + unit_y * wobble + rng.uniform(-2, 2))))
    points[-1] = (x1, y1)
    return points

def move_cursor(call, session, x, y):
    """Send the cursor to (x, y) along a human path, remembering where it rested."""
    state = _session_state.setdefault(session, {})
    x0, y0 = state.get("cursor") or (random.randint(160, 960), random.randint(120, 660))
    state["cursor"] = (x, y)
    points = path_points(x0, y0, x, y)
    for point_x, point_y in points[:-1]:
        call("Input.dispatchMouseEvent", type="mouseMoved", x=point_x, y=point_y)
        time.sleep(random.uniform(0.010, 0.026))
    if points:
        call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class OriginBusy(ValueError):
    """Another live agent holds this site's origin lock. Work elsewhere or wait longer."""


# Cap stored evidence so a noisy page cannot grow memory unbounded; counts keep rising.
ERROR_ITEM_LIMIT = 100
REQUEST_URL_LIMIT = 1000

# Main-document responses that prove the session is dead. A 200 login page is a soft
# signal; it only counts when JEV_LOGIN_URL_PATTERN names it.
AUTH_FAILURE_STATUSES = (401, 403)


def _desired_endpoint_base():
    """http(s) base of the browser BU_CDP_WS/BU_CDP_URL names, or None for local discovery."""
    raw = (os.environ.get("BU_CDP_WS") or os.environ.get("BU_CDP_URL") or "").strip()
    if not raw:
        return None
    scheme, _, rest = raw.partition("://")
    base = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    if base not in ("http", "https") or not rest:
        raise ValueError(f"BU_CDP_WS/BU_CDP_URL must be a ws:// or http:// endpoint, got {raw!r}")
    return f"{base}://{rest.split('/', 1)[0]}"


def _endpoint_page_ids(endpoint):
    """Page target IDs at a DevTools HTTP endpoint. Chrome mints target IDs per
    browser instance, so these identify the exact Chrome serving the endpoint."""
    with urllib.request.urlopen(f"{endpoint}/json/list", timeout=5) as response:
        return {target["id"] for target in json.loads(response.read()) if target.get("type") == "page"}


def ensure_isolated_daemon():
    """Attach to a daemon that serves the browser this session's environment names.

    Daemons are keyed by BU_NAME (default "default") and a daemon serves exactly
    one Chrome. A second process that sets only BU_CDP_WS/BU_CDP_URL would
    silently reuse a live daemon's browser — its profile cookies, and session
    healing or cache clears included. Disjoint page target IDs between the live
    daemon and the requested endpoint prove two different Chrome instances; that
    silent-sharing case fails loudly instead."""
    endpoint = _desired_endpoint_base()
    if endpoint is None or not daemon_alive():
        ensure_daemon()
        return
    try:
        wanted = _endpoint_page_ids(endpoint)
    except Exception as error:
        raise RuntimeError(
            f"{endpoint} from BU_CDP_WS/BU_CDP_URL is not reachable ({error}). "
            "Start the Chrome that serves it, or unset both variables to use local discovery."
        ) from error
    try:
        attached = {info.get("targetId") for info in cdp("Target.getTargets").get("targetInfos", [])
                    if info.get("type") == "page"}
    except Exception:
        ensure_daemon()  # Stale daemon; its self-heal replaces it with one bound to our env.
        return
    if wanted & attached:
        ensure_daemon()
        return
    raise RuntimeError(
        f"A browser-harness daemon is already serving a different browser than {endpoint}. "
        "Sharing its BU_NAME would drive one Chrome from both sessions: the same profile "
        "cookies, and each session's healing or cache clears landing in the other's tab. "
        "Give this session its own browser: `python scripts/automation_chrome.py --profile <name>`, "
        "then export BU_NAME=<name> and BU_CDP_URL=<its port>. If sharing is intended, stop the "
        "other session or its daemon first (`browser-harness --reload`)."
    )


class Browser:
    def __init__(self, url, *, foreground=False, keep_open=False, collect_errors=True,
                 heal_session=True, origin_wait=0, lock_origin=True):
        # The origin lock comes first: a busy site is refused before any daemon,
        # tab, or billed work happens. Automatic healing never clears a site
        # another live agent holds (heal() checks contested() at clear time).
        lock = OriginLock(urlparse(url).netloc) if lock_origin else None
        if lock is not None and not lock.acquire(wait_s=origin_wait):
            raise OriginBusy(
                f"another live agent holds {urlparse(url).netloc}; "
                "pass a longer --origin-wait to queue, or run a different origin"
            )
        try:
            ensure_isolated_daemon()
            self.keep_open = keep_open
            self.target = cdp("Target.createTarget", url="about:blank", background=not foreground)["targetId"]
            try:
                self._attach(url, collect_errors, heal_session)
            except BaseException:
                # A wedged page must not leak a tab in unattended batch runs.
                try:
                    cdp("Target.closeTarget", targetId=self.target)
                except Exception:
                    pass
                self.target = None
                raise
        except BaseException:
            if lock is not None:
                lock.release()
            raise
        self.origin_lock = lock

    def _attach(self, url, collect_errors, heal_session):
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        # A fresh plausible window size per run; recordings pin theirs with JEV_VIEWPORT.
        width, height = viewport_for_run()
        _session_state[self.session] = {"viewport": (width, height)}
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        # Error evidence: console errors, uncaught exceptions, failed/4xx+ requests. A model
        # judgment of DONE is not proof; these events are.
        self.collect_errors = collect_errors
        # The tab shares the attached Chrome profile, so cookies and storage persist across
        # runs like a normal browser. Healing never touches that profile without evidence.
        self.heal_session = heal_session
        self.errors = []
        self.error_counts = {}
        self._request_urls = {}
        self.document_status = None
        self.document_url = ""
        self.session_events = []
        self._network_enabled = bool(collect_errors or heal_session)
        if self.collect_errors:
            if os.environ.get("JEV_RUNTIME_ERRORS") == "1":
                # Deep capture for debugging; pages can observe Runtime.enable.
                self.call("Runtime.enable")
            else:
                # Passive in-page collectors keep exception evidence without Runtime.enable.
                # Without Page.enable the injection below is silently ignored.
                self.call("Page.enable")
                self.call("Page.addScriptToEvaluateOnNewDocument", source=PAGE_ERROR_COLLECTOR)
            # Log entries are browser-side records (network, CSP); enabling the
            # domain changes nothing the page can see.
            self.call("Log.enable")
        if self._network_enabled:
            self.call("Network.enable")
        username = os.environ.get("JEV_BASIC_AUTH_USERNAME")
        password = os.environ.get("JEV_BASIC_AUTH_PASSWORD")
        if (username is None) != (password is None):
            raise ValueError("JEV_BASIC_AUTH_USERNAME and JEV_BASIC_AUTH_PASSWORD must be set together")
        # Credentials serve one origin: JEV_BASIC_AUTH_ORIGIN (comma-separated) when set,
        # otherwise the landing origin. Every other origin skips interception entirely,
        # so credentials added for one protected host never touch unrelated runs.
        origin = urlparse(url).netloc
        scope = {s.strip() for s in os.environ.get("JEV_BASIC_AUTH_ORIGIN", "").split(",") if s.strip()}
        self.auth_origin = origin if username is not None and (not scope or origin in scope) else None
        self.auth_username = username
        self.auth_password = password
        if self.auth_origin:
            # Intercept document navigations only, at the request stage. Intercepting every
            # response of an origin stalls SPA data calls: the app shell loads, its XHRs hang
            # off-page, and the loop observes a permanently blank page. Answering one document
            # challenge caches the credentials, which Chrome then attaches to same-origin
            # subresources itself.
            self.call(
                "Fetch.enable",
                handleAuthRequests=True,
                patterns=[{"urlPattern": f"https://{self.auth_origin}/*",
                           "requestStage": "Request", "resourceType": "Document"}],
            )
        self._load(url)
        if heal_session:
            self.heal(url)

    def _load(self, url):
        try:
            self.call("Page.navigate", url=url)
        except Exception:
            # Fetch auth challenges pause navigation until answered. The harness
            # waits synchronously for Page.navigate, so an authenticated load can
            # time out here even though its challenge is queued for drain_events.
            if not self.auth_origin:
                raise
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self._drain()
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    def _record(self, kind, text, url=""):
        self.error_counts[kind] = self.error_counts.get(kind, 0) + 1
        if len(self.errors) < ERROR_ITEM_LIMIT:
            self.errors.append({"kind": kind, "text": text[:300], "url": url[:300]})

    # One drain point for the session: auth challenges must not be starved by error
    # collection, so both route through the same pass over the daemon event queue.
    def _drain(self):
        if not self.auth_origin and not self.collect_errors:
            return
        for event in drain_events():
            if event.get("session_id") != self.session:
                continue
            method = event.get("method")
            params = event.get("params", {})
            if method == "Fetch.authRequired":
                challenge_url = urlparse(params.get("request", {}).get("url", ""))
                response = "ProvideCredentials" if challenge_url.netloc == self.auth_origin else "CancelAuth"
                self.call(
                    "Fetch.continueWithAuth",
                    requestId=params["requestId"],
                    authChallengeResponse={
                        "response": response,
                        **({"username": self.auth_username, "password": self.auth_password}
                           if response == "ProvideCredentials" else {}),
                    },
                )
            elif method == "Fetch.requestPaused":
                try:
                    self.call("Fetch.continueRequest", requestId=params["requestId"])
                except RuntimeError as error:
                    if "Invalid InterceptionId" not in str(error):
                        raise
            elif method == "Runtime.exceptionThrown":
                details = params.get("exceptionDetails", {})
                self._record("exception", details.get("exception", {}).get("description") or details.get("text", ""))
            elif method == "Runtime.consoleAPICalled":
                if params.get("type") == "error":
                    text = " | ".join(
                        str(a.get("value") if a.get("value") is not None else a.get("description") or "")
                        for a in params.get("args", [])
                    )
                    self._record("console", text)
            elif method == "Log.entryAdded":
                entry = params.get("entry", {})
                if entry.get("level") == "error":
                    self._record("console", entry.get("text", ""), entry.get("url", ""))
            elif method == "Network.requestWillBeSent":
                if len(self._request_urls) >= REQUEST_URL_LIMIT:
                    self._request_urls.clear()
                self._request_urls[params.get("requestId")] = params.get("request", {}).get("url", "")
            elif method == "Network.loadingFailed":
                self._record("request_failed", params.get("errorText", ""),
                             self._request_urls.get(params.get("requestId"), ""))
            elif method == "Network.responseReceived":
                response = params.get("response", {})
                if params.get("type") == "Document":
                    self.document_status = response.get("status")
                    self.document_url = response.get("url", "")
                if response.get("status", 0) >= 400:
                    self._record("http_status", f"HTTP {response.get('status')}", response.get("url", ""))

    def error_summary(self):
        return {"counts": dict(self.error_counts), "items": list(self.errors)}

    def session_ok(self, url):
        """Read-only verdict that the loaded session still works. Hard signals are
        401/403 on the main document; a 200 login wall only counts when the
        JEV_LOGIN_URL_PATTERN regex names its URL. No signal means keep cookies."""
        if self.document_status in AUTH_FAILURE_STATUSES:
            return False
        pattern = os.environ.get("JEV_LOGIN_URL_PATTERN", "").strip()
        if pattern and re.search(pattern, self.document_url or url):
            return False
        return True

    def heal(self, url):
        """Behave like a person with a normal browser: keep cookies, reload once,
        and only clear this origin's cookies and cache when the session still
        looks dead. Other sites' logins and the rest of the profile stay intact."""
        if self.session_ok(url):
            return
        self.session_events = ["landing looks logged out"]
        self._load(url)
        if self.session_ok(url):
            self.session_events.append("reload restored the session")
            return
        stale = self.cookies(url)
        if OriginLock.contested(urlparse(url).netloc):
            # Another live agent is working on this site; clearing its cookies
            # would sign that agent out mid-task. A dead holder's lock is gone,
            # so this never blocks recovery of an abandoned session.
            self.session_events.append("another agent holds this origin; cookies kept")
            return
        self.clear_cookies(url)
        self.clear_cache()
        origin = urlparse(self.document_url or url).netloc
        self.session_events.append(f"cleared {len(stale)} cookie(s) and cache for {origin}")
        self._load(url)
        pattern = os.environ.get("JEV_LOGIN_URL_PATTERN", "").strip()
        if pattern and not self.session_ok(url):
            self.session_events.append("still behind the login wall; sign in once in the tab")
        else:
            self.session_events.append("origin storage cleared; the next sign-in persists")

    def _ensure_network(self):
        if not self._network_enabled:
            self.call("Network.enable")
            self._network_enabled = True

    def _maintenance_call(self, method, **params):
        # Cache eviction can sweep a full disk cache; the default 5s IPC response
        # timeout is too tight for those round trips.
        return cdp(method, session_id=self.session, _response_timeout=30, **params)

    def cookies(self, url=None):
        """Cookie records for one origin, or every cookie in the profile with url=None."""
        self._ensure_network()
        if url is None:
            return self._maintenance_call("Storage.getCookies").get("cookies", [])
        parsed = urlparse(url)
        return self._maintenance_call(
            "Network.getCookies", urls=[f"{parsed.scheme or 'https'}://{parsed.netloc}", url]
        ).get("cookies", [])

    def clear_cookies(self, url=None):
        """Delete cookies for one origin (default) or every origin with url=None."""
        self._ensure_network()
        if url is None:
            self._maintenance_call("Network.clearBrowserCookies")
            return
        for cookie in self.cookies(url):
            params = {"name": cookie["name"], "path": cookie.get("path", "/")}
            if cookie.get("domain"):
                params["domain"] = cookie["domain"]
            self._maintenance_call("Network.deleteCookies", **params)

    def clear_cache(self):
        """Empty the HTTP cache. Cookies and site storage are untouched."""
        self._ensure_network()
        self._maintenance_call("Network.clearBrowserCache")

    def save_cookies(self, url, path):
        """Write one origin's cookies and localStorage to a JSON snapshot; returns the cookie count.

        Snapshot files hold live session credentials: keep them under the user
        home (the default in scripts/session.py), never inside a repository.
        localStorage is captured best-effort — the tab must currently be on the
        origin, which it is right after a normal load.
        """
        cookies = self.cookies(url)
        local_storage = None
        try:
            origin = urlparse(self.evaluate("location.href") or "").netloc
            if origin == urlparse(url).netloc:
                local_storage = self.evaluate(
                    "(() => { const out={}; for (let i=0;i<localStorage.length;i++) "
                    "{ const k=localStorage.key(i); out[k]=localStorage.getItem(k); } return out; })()")
        except (StalePage, RuntimeError, ValueError, AttributeError):
            local_storage = None
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(
            {"origin": url, "saved_at": int(time.time()), "cookies": cookies,
             "local_storage": local_storage},
            indent=2,
        ), encoding="utf-8")
        return len(cookies)

    def restore_cookies(self, path):
        """Load a snapshot written by save_cookies; returns the cookie count.

        localStorage items are replayed only when the tab currently sits on the
        snapshot's origin, so scripts/session.py loads the URL first.
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        if cookies:
            self._ensure_network()
            self._maintenance_call("Storage.setCookies", cookies=cookies)
        items = data.get("local_storage") or {}
        if items:
            try:
                origin = urlparse(self.evaluate("location.href") or "").netloc
                if origin == urlparse(data.get("origin", "")).netloc:
                    self.evaluate("(() => { const items = " + json.dumps(items) + ";"
                                  " for (const [k, v] of Object.entries(items)) localStorage.setItem(k, v);"
                                  " return Object.keys(items).length; })()")
            except (StalePage, RuntimeError, ValueError):
                pass
        return len(cookies)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        self._drain()
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression=_js("""(action => new Promise(resolve => {
                      const field=window[__JEV_KEY__]?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""") + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        info = None
        for attempt in range(10):
            try:
                info = browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
                break
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        self._ingest_page_errors(info)
        return info

    def _ingest_page_errors(self, info):
        """Collector events become evidence like protocol events; the in-page copy clears after."""
        page_errors = info.pop("page_errors", None) if info else None
        if not page_errors:
            return
        for item in page_errors:
            self._record(item.get("kind", "exception"), item.get("text", ""), item.get("url", ""))
        try:
            self.evaluate(_js("window[__JEV_KEY__].page_errors.length=0"))
        except (StalePage, RuntimeError):
            pass

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select", "file"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                _js("(() => { const c=window[__JEV_KEY__]; ")
                + f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None, files=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action,
                                    "text": text, "files": files})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        # A close-time daemon hiccup must not crash the batch run that owns this tab;
        # the target dies with Chrome regardless.
        if self.target and not self.keep_open:
            try:
                cdp("Target.closeTarget", targetId=self.target)
            except Exception:
                pass
        if getattr(self, "session", None):
            _session_state.pop(self.session, None)
        self.target = None
        if getattr(self, "origin_lock", None) is not None:
            self.origin_lock.release()
            self.origin_lock = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            # The wheel rests somewhere, ticks in notches with a decaying tail, and the
            # hand trembles a little between ticks.
            wheel_x, wheel_y = scroll_point(session)
            move_cursor(call, session, wheel_x, wheel_y)
            for index, notch in enumerate(wheel_plan(action["delta"])):
                if index:
                    time.sleep(min(0.12, random.uniform(0.02, 0.04) * (1.25 ** index)))
                call("Input.dispatchMouseEvent", type="mouseWheel",
                     x=max(0, wheel_x + random.randint(-6, 6)),
                     y=max(0, wheel_y + random.randint(-4, 4)),
                     deltaX=0, deltaY=notch)
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            if kind == "file" and not (request.get("files") or []):
                raise ValueError("SET_FILE needs operator-provided file paths; none were given.")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate(_js("""(action => {
              const e=window[__JEV_KEY__]?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!e.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT') return null;
                const selectable=[...e.options].filter(o=>!o.disabled && !o.closest('optgroup[disabled]'));
                const option=selectable.find(o=>o.value===action.value);
                if (!option) return null;
                const current=e.selectedOptions[0] ? selectable.indexOf(e.selectedOptions[0]) : -1;
                return {x,y,steps:selectable.indexOf(option)-current};
              }
              return {x,y};
            })(""") + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind == "file":
                # The chooser is bypassed entirely: the file comes from the operator,
                # never from model output, and the change event tells the page.
                files = [str(f) for f in request["files"]]
                if not files:
                    raise ValueError("SET_FILE needs operator-provided file paths; none were given.")
                remote = call(
                    "Runtime.evaluate",
                    expression=f"window[{_KEY_JSON}].nodes.get({action['node']})",
                    returnByValue=False,
                    objectGroup="jev-setfile",
                )
                object_id = (remote.get("result") or {}).get("objectId")
                if not object_id:
                    raise StalePage("File target vanished. Observe again.")
                call("DOM.enable")
                # requestNode only resolves once the DOM agent has a document.
                call("DOM.getDocument", depth=0)
                node = call("DOM.requestNode", objectId=object_id)["nodeId"]
                call("DOM.setFileInputFiles", files=files, nodeId=node)
                call("Runtime.releaseObjectGroup", objectGroup="jev-setfile")
                set_count = evaluate(
                    f"(() => {{ const e = window[{_KEY_JSON}].nodes.get({action['node']});"
                    " e.dispatchEvent(new Event('input',{bubbles:true}));"
                    " e.dispatchEvent(new Event('change',{bubbles:true}));"
                    " return e.files.length; })()"
                )
                if set_count != len(files):
                    raise StalePage("File attachment was not confirmed; inspect before retrying.")
            elif kind == "select":
                steps = int(target.get("steps") or 0)
                committed = steps == 0
                if steps and sys.platform != "darwin":
                    # Arrows on a focused select commit through native trusted input/change events.
                    try:
                        evaluate(_js(f"window[__JEV_KEY__].nodes.get({action['node']}).focus()"))
                        direction = "Down" if steps > 0 else "Up"
                        for _ in range(abs(steps)):
                            params = arrow_params(direction)
                            call("Input.dispatchKeyEvent", type="keyDown", **params)
                            call("Input.dispatchKeyEvent", type="keyUp", **params)
                            time.sleep(random.uniform(0.004, 0.02))
                        committed = evaluate(_js(
                            f"window[__JEV_KEY__].nodes.get({action['node']})?.value === "
                            + json.dumps(action["value"])))
                    except (StalePage, RuntimeError):
                        committed = False
                if not committed and not evaluate(_js(
                        "(node => { const e=window[__JEV_KEY__].nodes.get(node);"
                        " if (!e) return false;"
                        " e.value=" + json.dumps(action["value"]) + ";"
                        " e.dispatchEvent(new Event('input',{bubbles:true}));"
                        " e.dispatchEvent(new Event('change',{bubbles:true}));"
                        " return e.value===" + json.dumps(action["value"]) + ";"
                        f"}})({action['node']})")):
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
            else:
                x, y = target["x"], target["y"]
                # The hand travels to the control along a curved path before it presses.
                move_cursor(call, session, x, y)
                time.sleep(random.uniform(0.008, 0.03))
                call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
                time.sleep(random.uniform(0.015, 0.06))
                call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    mode, body = typing_plan(request["text"] or "")
                    if mode == "keys":
                        gaps = typing_gaps(body)
                        for index, char in enumerate(body):
                            call("Input.dispatchKeyEvent", type="keyDown", **char_key_params(char))
                            call("Input.dispatchKeyEvent", type="keyUp", **char_key_params(char, press=False))
                            if index < len(gaps):
                                time.sleep(gaps[index])
                    elif request["text"]:
                        call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
