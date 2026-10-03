"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp, drain_events

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


# Cap stored evidence so a noisy page cannot grow memory unbounded; counts keep rising.
ERROR_ITEM_LIMIT = 100
REQUEST_URL_LIMIT = 1000

# Main-document responses that prove the session is dead. A 200 login page is a soft
# signal; it only counts when JEV_LOGIN_URL_PATTERN names it.
AUTH_FAILURE_STATUSES = (401, 403)


class Browser:
    def __init__(self, url, *, foreground=False, keep_open=False, collect_errors=True, heal_session=True):
        ensure_daemon()
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

    def _attach(self, url, collect_errors, heal_session):
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
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
            self.call("Runtime.enable")
            self.call("Log.enable")
        if self._network_enabled:
            self.call("Network.enable")
        username = os.environ.get("JEV_BASIC_AUTH_USERNAME")
        password = os.environ.get("JEV_BASIC_AUTH_PASSWORD")
        if (username is None) != (password is None):
            raise ValueError("JEV_BASIC_AUTH_USERNAME and JEV_BASIC_AUTH_PASSWORD must be set together")
        self.auth_origin = urlparse(url).netloc if username is not None else None
        self.auth_username = username
        self.auth_password = password
        if self.auth_origin:
            self.call(
                "Fetch.enable",
                handleAuthRequests=True,
                patterns=[{"urlPattern": f"https://{self.auth_origin}/*", "requestStage": "Response"}],
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
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
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
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        if self.target and not self.keep_open:
            cdp("Target.closeTarget", targetId=self.target)
        self.target = None


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
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!e.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
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
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
