"""End-to-end auth lifecycle against a staging deployment, driven over raw CDP.

signup -> app home -> sign out -> sign in (returning user) -> app home.
Verification codes are read from the Resend inbox for the test address, so the
flow never hardcodes a code. Every tunable comes from the environment:

  JEV_E2E_BASE_URL      app origin under test (e.g. https://staging.example.com)
  JEV_E2E_EMAIL         test inbox address the signup/signin forms submit
  E2E_RESEND_API_KEY    Resend key used to poll that inbox for the code
  JEV_E2E_AUTH_ORIGINS  optional comma-separated extra origins whose cookies are
                        cleared before signup (e.g. an SSO host)
  JEV_CDP_URL           DevTools endpoint (default http://127.0.0.1:9333)

  uv run --env-file .env python scripts/e2e_auth.py             # full loop
  uv run --env-file .env python scripts/e2e_auth.py --skip-signup  # signin only
"""

import asyncio
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

import websockets

CDP = (os.environ.get("JEV_CDP_URL") or "http://127.0.0.1:9333").rstrip("/")
BASE_URL = (os.environ.get("JEV_E2E_BASE_URL") or "").rstrip("/")
EMAIL = os.environ.get("JEV_E2E_EMAIL") or ""
RESEND_KEY = os.environ.get("E2E_RESEND_API_KEY") or ""
AUTH_ORIGINS = [o.strip() for o in (os.environ.get("JEV_E2E_AUTH_ORIGINS") or "").split(",") if o.strip()]
SKIP_SIGNUP = "--skip-signup" in sys.argv

missing = [name for name, value in [
    ("JEV_E2E_BASE_URL", BASE_URL), ("JEV_E2E_EMAIL", EMAIL), ("E2E_RESEND_API_KEY", RESEND_KEY),
] if not value]
if missing:
    raise SystemExit(f"e2e_auth: missing required env: {', '.join(missing)}")

SINCE = time.time() * 1000 - 60_000  # ignore codes older than a minute


def _created_age(created):
    """Seconds since the Resend created_at stamp; unparseable stamps count as fresh."""
    if not created:
        return 0.0
    try:
        return time.time() - datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def resend_code():
    """Poll the Resend inbox for the newest 6-digit code sent to the test address."""
    deadline = time.time() + 90
    # The API 403s a bare python-urllib User-Agent; keep the browser-ish one.
    headers = {"Authorization": f"Bearer {RESEND_KEY}", "User-Agent": "Mozilla/5.0",
               "Accept": "application/json"}
    while time.time() < deadline:
        try:
            data = json.load(urllib.request.urlopen(
                urllib.request.Request("https://api.resend.com/emails?limit=10", headers=headers), timeout=10)
            ).get("data", [])
            for mail in data:
                to = mail["to"][0] if isinstance(mail["to"], list) else mail["to"]
                if to.lower() == EMAIL.lower() and _created_age(mail.get("created_at", "")) < 300:
                    full = json.load(urllib.request.urlopen(urllib.request.Request(
                        f"https://api.resend.com/emails/{mail['id']}", headers=headers), timeout=10))
                    match = re.search(r"\b(\d{6})\b", full.get("text") or full.get("html") or "")
                    if match:
                        return match.group(1)
        except Exception as exc:
            print("  resend poll:", exc)
        time.sleep(4)
    return None


class Tab:
    """One CDP tab driven with direct protocol calls; no agent, no model."""

    def __init__(self, websocket):
        self.ws = websocket
        self.mid = 0
        self.page = None

    async def call(self, method, params=None):
        self.mid += 1
        await self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=40))
            if message.get("id") == self.mid:
                if "error" in message:
                    return "<cdperr> " + str(message["error"].get("message", ""))[:120]
                return message.get("result", {})

    async def ev(self, expr):
        result = await self.call("Runtime.evaluate",
                                 {"expression": expr, "returnByValue": True, "awaitPromise": True})
        value = result.get("result", {})
        if value.get("subtype") == "error":
            return "<jserr> " + str(value.get("description", ""))[:140]
        return value.get("value")

    async def body(self, chars=180):
        return str(await self.ev(f"document.body.innerText.slice(0,{chars})")).replace("\n", " | ")

    async def url(self):
        return str(await self.ev("location.href"))

    async def wait_url(self, fragment, tries=15, pause=4):
        for _ in range(tries):
            url = await self.url()
            if fragment in url:
                return url
            await asyncio.sleep(pause)
        return await self.url()

    async def set_input(self, pick, value):
        return await self.ev(
            "(() => { const el = " + pick + "; if (!el) return 'no-el';"
            " const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype"
            " : window.HTMLInputElement.prototype;"
            " const set = Object.getOwnPropertyDescriptor(proto, 'value').set;"
            " el.focus(); set.call(el, " + json.dumps(value) + ");"
            " el.dispatchEvent(new Event('input', {bubbles:true}));"
            " el.dispatchEvent(new Event('change', {bubbles:true})); el.blur(); return 'ok'; })()")

    async def click_btn(self, pattern):
        return await self.ev(
            "[...document.querySelectorAll('button, a, [role=button]')].find(b => /"
            + pattern + "/i.test(b.textContent))?.click() ?? 'none'")

    async def open(self, url, settle=9):
        """Fresh tab; clears app + SSO cookies so a stale session cannot leak into signup."""
        import urllib.request
        probe = json.load(urllib.request.urlopen(
            urllib.request.Request(f"{CDP}/json/new?about:blank", method="PUT"), timeout=10))
        async with websockets.connect(probe["webSocketDebuggerUrl"], max_size=2 ** 22) as sweeper:
            mid = 0
            for origin in AUTH_ORIGINS + [BASE_URL]:
                mid += 1
                await sweeper.send(json.dumps({
                    "id": mid, "method": "Storage.clearDataForOrigin",
                    "params": {"origin": origin, "storageTypes": "cookies"}}))
                while True:
                    message = json.loads(await asyncio.wait_for(sweeper.recv(), timeout=10))
                    if message.get("id") == mid:
                        break
        urllib.request.urlopen(f"{CDP}/json/close/{probe['id']}", timeout=10)
        raw = urllib.request.urlopen(
            urllib.request.Request(f"{CDP}/json/new?about:blank", method="PUT"), timeout=10)
        self.page = json.load(raw)
        self.ws = await websockets.connect(self.page["webSocketDebuggerUrl"], max_size=2 ** 24)
        await self.call("Page.enable")
        await self.call("Page.navigate", {"url": BASE_URL + "/"})
        await asyncio.sleep(5)
        await self.ev("location.assign(" + json.dumps(url) + "); 'nav'")
        await asyncio.sleep(settle)

    async def close(self):
        import urllib.request
        try:
            await self.ws.close()
        except Exception:
            pass
        if self.page:
            try:
                urllib.request.urlopen(f"{CDP}/json/close/{self.page['id']}", timeout=10)
            except Exception:
                pass


async def fill_code(tab, code):
    return await tab.ev(
        "(() => { const code = " + json.dumps(code) + ";"
        " const digits = [...document.querySelectorAll('input')].filter(i => i.maxLength === 1);"
        " if (digits.length >= 6) { const set = Object.getOwnPropertyDescriptor("
        "window.HTMLInputElement.prototype, 'value').set;"
        " digits.slice(0,6).forEach((el,i) => { el.focus(); set.call(el, code[i]);"
        " el.dispatchEvent(new Event('input', {bubbles:true})); }); return 'digits'; }"
        " const single = document.querySelector('input[inputmode=numeric], input[autocomplete=one-time-code]');"
        " if (single) { const set = Object.getOwnPropertyDescriptor("
        "window.HTMLInputElement.prototype, 'value').set;"
        " single.focus(); set.call(single, code); single.dispatchEvent(new Event('input', {bubbles:true}));"
        " return 'single'; } return 'none'; })()")


async def complete_code_step(tab):
    """Code screen shared by signup and signin: fetch the code, fill, continue."""
    code = await asyncio.get_event_loop().run_in_executor(None, resend_code)
    print(" CODE:", code)
    if not code:
        return False
    print(" code-fill:", await fill_code(tab, code))
    await asyncio.sleep(6)
    print(" screen:", await tab.body(80))
    if "Continue" in await tab.body(200):
        print(" continue:", await tab.click_btn("continue"))
        await asyncio.sleep(5)
    return True


async def signup(tab):
    print("== SIGNUP ==")
    print(" screen:", await tab.body(70))
    print(" email:", await tab.set_input(
        "[...document.querySelectorAll('input')].find(i => i.type !== 'checkbox' && i.type !== 'submit')", EMAIL))
    await asyncio.sleep(0.8)
    print(" submit:", await tab.click_btn("create account"))
    await asyncio.sleep(6)
    print(" screen:", await tab.body(70))
    if not await complete_code_step(tab):
        return False
    # Optional extra-profile step (name + birthdate); present on some deployments only.
    print(" profile:", await tab.ev("""(() => {
      const set = (el, v) => { if (!el) return '-'; const s = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value').set; el.focus(); s.call(el, v);
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true})); el.blur(); return 'ok'; };
      const ins = [...document.querySelectorAll('input')];
      const given = ins.find(i => i.name === 'givenName');
      if (!given) return 'not-profile-step';
      return ['Alex', 'Chen'].map((v, i) => set([given, ins.find(x => x.name === 'familyName')][i], v)).join(',')
        + ',' + [2, 3, 4].map(idx => set(ins[idx], ['06', '15', '1995'][idx - 2])).join(',');
    })()"""))
    await asyncio.sleep(1.2)
    print(" continue:", await tab.click_btn("continue"))
    if await tab.wait_url("create-passkey", tries=6):
        print(" passkey screen reached")
    print(" skip:", await tab.ev(
        "(() => { const el = [...document.querySelectorAll('*')].find("
        "e => e.children.length === 0 && e.textContent.trim() === 'Skip'); if (!el) return 'none';"
        " el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true})); return 'skipped'; })()"))
    final = await tab.wait_url(BASE_URL, tries=15)
    print(" landed:", final[:80])
    print(" body:", await tab.body(150))
    return "/home" in final


async def signout(tab):
    print("== SIGN OUT ==")
    if "/home" not in await tab.url():
        await tab.ev("location.assign(" + json.dumps(BASE_URL + "/home") + "); 'nav'")
        await asyncio.sleep(7)
    print(" click:", await tab.click_btn("sign out"))
    await asyncio.sleep(6)
    print(" url:", (await tab.url())[:80])
    body = await tab.body(300)
    print(" body:", body[:200])
    return ("Sign in" in body or "Guest" in body) and "Your resumes" not in body[:120]


async def signin(tab):
    print("== SIGN IN (returning) ==")
    await tab.ev("location.assign(" + json.dumps(BASE_URL + "/login?next=/home") + "); 'nav'")
    await asyncio.sleep(10)
    print(" screen:", await tab.body(70))
    print(" email:", await tab.set_input(
        "[...document.querySelectorAll('input')].find(i => i.type !== 'checkbox' && i.type !== 'submit')", EMAIL))
    await asyncio.sleep(0.8)
    print(" submit:", await tab.click_btn("sign in|continue"))
    await asyncio.sleep(6)
    print(" screen:", await tab.body(80))
    if not await complete_code_step(tab):
        return False
    final = await tab.wait_url(BASE_URL, tries=15)
    print(" landed:", final[:80])
    print(" body:", await tab.body(200))
    return "/home" in final


async def main():
    tab = Tab(None)
    try:
        if not SKIP_SIGNUP:
            await tab.open(BASE_URL + "/signup")
            print("SIGNUP RESULT:", "PASS" if await signup(tab) else "FAIL")
            ok_out = await signout(tab)
            print("SIGNOUT RESULT:", "PASS" if ok_out else "FAIL")
        else:
            await tab.open(BASE_URL + "/login?next=/home")
        ok_in = await signin(tab)
        print("SIGNIN RESULT:", "PASS" if ok_in else "FAIL")
    finally:
        await tab.close()


asyncio.run(main())
