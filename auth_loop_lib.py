"""Complete auth loop on staging: signup -> /home -> sign out -> sign in (returning user) -> /home.

Run: cd jev-ultrafast && uv run --env-file .env --frozen python auth-loop.py
"""
import asyncio
import json
import re
import time
import urllib.request
import websockets
from pathlib import Path

CDP = "http://127.0.0.1:9333"
ENV = Path(r"C:\Nodesify\gitlab\resume-builder\.env").read_text().splitlines()
RESEND_KEY = [l.split("=", 1)[1].strip() for l in ENV if l.startswith("E2E_RESEND_API_KEY=")][0]
RH = {"Authorization": "Bearer " + RESEND_KEY, "User-Agent": "Mozilla/5.0", "Accept": "application/json"}
EMAIL = "staging-accept2-20261002@nodesify.com"


def resend_code():
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            data = json.load(urllib.request.urlopen(urllib.request.Request(
                "https://api.resend.com/emails?limit=5", headers=RH), timeout=10)).get("data", [])
            for e in data:
                to = e["to"][0] if isinstance(e["to"], list) else e["to"]
                if to.lower() == EMAIL:
                    full = json.load(urllib.request.urlopen(urllib.request.Request(
                        f"https://api.resend.com/emails/{e['id']}", headers=RH), timeout=10))
                    m = re.search(r"\b(\d{6})\b", full.get("text") or full.get("html") or "")
                    if m:
                        return m.group(1)
        except Exception as exc:
            print("  resend poll:", exc)
        time.sleep(4)
    return None


class Tab:
    def __init__(self, ws):
        self.ws = ws
        self.mid = 0

    async def call(self, method, params=None):
        self.mid += 1
        await self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params or {}}))
        while True:
            m = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=40))
            if m.get("id") == self.mid:
                if "error" in m:
                    return "<cdperr> " + str(m["error"].get("message", ""))[:120]
                return m.get("result", {})

    async def ev(self, expr):
        r = await self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        res = r.get("result", {})
        if res.get("subtype") == "error":
            return "<jserr> " + str(res.get("description", ""))[:140]
        return res.get("value")

    async def body(self, n=180):
        v = await self.ev(f"document.body.innerText.slice(0,{n})")
        return str(v).replace("\n", " | ")

    async def url(self):
        return str(await self.ev("location.href"))

    async def wait_url(self, fragment, tries=15, pause=4):
        for i in range(tries):
            u = await self.url()
            if fragment in u:
                return u
            await asyncio.sleep(pause)
        return await self.url()

    async def set_input(self, pick, value):
        return await self.ev(
            "(() => { const el = " + pick + "; if (!el) return 'no-el';"
            " const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;"
            " const set = Object.getOwnPropertyDescriptor(proto, 'value').set;"
            " el.focus(); set.call(el, " + json.dumps(value) + ");"
            " el.dispatchEvent(new Event('input', {bubbles:true}));"
            " el.dispatchEvent(new Event('change', {bubbles:true})); el.blur(); return 'ok'; })()")

    async def click_btn(self, pattern):
        return await self.ev(
            "[...document.querySelectorAll('button, a, [role=button]')].find(b => /"
            + pattern + "/i.test(b.textContent))?.click() ?? 'none'")

    async def new_tab(self, url):
        # clear Logto + staging cookies so no stale/deleted session interferes
        import urllib.request as _u
        probe = _u.urlopen(_u.Request(f"{CDP}/json/new?about:blank", method="PUT"))
        probe_page = json.load(probe)
        ws = await websockets.connect(probe_page["webSocketDebuggerUrl"], max_size=2**22)
        for origin in ("https://auth.nodesify.dev", "https://staging.resumeforu.com"):
            await ws.send(json.dumps({"id": 1, "method": "Storage.clearDataForOrigin",
                                      "params": {"origin": origin, "storageTypes": "cookies"}}))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if m.get("id") == 1:
                    break
        await ws.close()
        _u.urlopen(f"{CDP}/json/close/{probe_page['id']}")
        raw = urllib.request.urlopen(urllib.request.Request(f"{CDP}/json/new?about:blank", method="PUT"))
        page = json.load(raw)
        self.page = page
        self.ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=2**24)
        await self.call("Page.enable")
        await self.call("Page.navigate", {"url": "https://staging.resumeforu.com/"})
        await asyncio.sleep(5)
        await self.ev("location.assign(" + json.dumps(url) + "); 'nav'")
        await asyncio.sleep(9)

    async def close(self):
        try:
            asyncio.get_event_loop().create_task(self.ws.close())
        except Exception:
            pass
        try:
            urllib.request.urlopen(f"{CDP}/json/close/{self.page['id']}")
        except Exception:
            pass


async def fill_code(tab, code):
    return await tab.ev(
        "(() => { const code = " + json.dumps(code) + ";"
        " const digits = [...document.querySelectorAll('input')].filter(i => i.maxLength === 1);"
        " if (digits.length >= 6) { const set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;"
        " digits.slice(0,6).forEach((el,i) => { el.focus(); set.call(el, code[i]); el.dispatchEvent(new Event('input', {bubbles:true})); }); return 'digits'; }"
        " const single = document.querySelector('input[inputmode=numeric], input[autocomplete=one-time-code]');"
        " if (single) { const set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;"
        " single.focus(); set.call(single, code); single.dispatchEvent(new Event('input', {bubbles:true})); return 'single'; }"
        " return 'none'; })()")


async def signup(tab):
    print("== SIGNUP ==")
    print(" screen:", await tab.body(70))
    print(" email:", await tab.set_input("[...document.querySelectorAll('input')].find(i => i.type !== 'checkbox' && i.type !== 'submit')", EMAIL))
    await asyncio.sleep(0.8)
    print(" submit:", await tab.click_btn("create account"))
    await asyncio.sleep(6)
    print(" screen:", await tab.body(70))
    loop = asyncio.get_event_loop()
    code = await loop.run_in_executor(None, resend_code)
    print(" CODE:", code)
    if not code:
        return False
    print(" code-fill:", await fill_code(tab, code))
    await asyncio.sleep(6)
    print(" screen:", await tab.body(80))
    if "Continue" in await tab.body(200):
        print(" continue:", await tab.click_btn("continue"))
        await asyncio.sleep(5)
    # extra-profile (name + MM/DD/YYYY positional)
    print(" profile:", await tab.ev("""(() => {
      const set = (el, v) => { if (!el) return '-'; const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set; el.focus(); s.call(el, v); el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); el.blur(); return 'ok'; };
      const ins = [...document.querySelectorAll('input')];
      const given = ins.find(i => i.name === 'givenName');
      if (!given) return 'not-profile-step';
      return ['Alex', 'Chen'].map((v, i) => set([given, ins.find(x => x.name === 'familyName')][i], v)).join(',') + ',' + [2, 3, 4].map(idx => set(ins[idx], ['06', '15', '1995'][idx - 2])).join(',');
    })()"""))
    await asyncio.sleep(1.2)
    print(" continue:", await tab.click_btn("continue"))
    if await tab.wait_url("create-passkey", tries=6):
        print(" passkey screen reached")
    print(" skip:", await tab.ev("(() => { const el = [...document.querySelectorAll('*')].find(e => e.children.length === 0 && e.textContent.trim() === 'Skip'); if (!el) return 'none'; el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true})); return 'skipped'; })()"))
    final = await tab.wait_url("staging.resumeforu.com", tries=15)
    print(" landed:", final[:80])
    print(" body:", await tab.body(150))
    return "/home" in final


async def signout(tab):
    print("== SIGN OUT ==")
    print(" click:", await tab.click_btn("sign out"))
    await asyncio.sleep(6)
    print(" url:", (await tab.url())[:80])
    print(" body:", await tab.body(200))
    b = await tab.body(300)
    return ("Sign in" in b or "Guest" in b) and "Your resumes" not in b[:120]


async def signin(tab):
    print("== SIGN IN (returning) ==")
    print(" screen:", await tab.body(70))
    print(" email:", await tab.set_input("[...document.querySelectorAll('input')].find(i => i.type !== 'checkbox' && i.type !== 'submit')", EMAIL))
    await asyncio.sleep(0.8)
    print(" submit:", await tab.click_btn("sign in|continue"))
    await asyncio.sleep(6)
    print(" screen:", await tab.body(80))
    loop = asyncio.get_event_loop()
    code = await loop.run_in_executor(None, resend_code)
    print(" CODE:", code)
    if not code:
        return False
    print(" code-fill:", await fill_code(tab, code))
    await asyncio.sleep(6)
    print(" screen:", await tab.body(80))
    if "Continue" in await tab.body(200):
        print(" continue:", await tab.click_btn("continue"))
        await asyncio.sleep(5)
    final = await tab.wait_url("staging.resumeforu.com", tries=15)
    print(" landed:", final[:80])
    print(" body:", await tab.body(200))
    return "/home" in final


async def main():
    tab = Tab(None)
    try:
        await tab.new_tab("https://staging.resumeforu.com/signup")
        ok_signup = await signup(tab)
        print("SIGNUP RESULT:", "PASS" if ok_signup else "FAIL")

        # go to /home and sign out (signup may land on /home already)
        if "/home" not in await tab.url():
            await tab.ev("location.assign('https://staging.resumeforu.com/home'); 'nav'")
            await asyncio.sleep(7)
        ok_out = await signout(tab)
        print("SIGNOUT RESULT:", "PASS" if ok_out else "FAIL")

        await tab.ev("location.assign('https://staging.resumeforu.com/login?next=/home'); 'nav'")
        await asyncio.sleep(10)
        ok_in = await signin(tab)
        print("SIGNIN RESULT:", "PASS" if ok_in else "FAIL")
    finally:
        await tab.close()
