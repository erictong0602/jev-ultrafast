"""Full Logto signup dance in ONE tab: signup -> code (via Resend API) ->
profile form -> passkey skip -> back on staging. Run with the checkout's venv.
"""
import asyncio
import json
import re
import sys
import time
import urllib.request
import websockets

import os
from pathlib import Path

RESEND_KEY = None
for line in Path(r"C:\Nodesify\gitlab\resume-builder\.env").read_text().splitlines():
    if line.startswith("E2E_RESEND_API_KEY="):
        RESEND_KEY = line.split("=", 1)[1]

EMAIL = "staging-accept-20261002@nodesify.com"
SINCE = time.time() * 1000 - 60_000


def resend_code():
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            req = urllib.request.Request("https://api.resend.com/emails?limit=10",
                                         headers={"Authorization": f"Bearer {RESEND_KEY}"})
            data = json.load(urllib.request.urlopen(req, timeout=10)).get("data", [])
            for e in data:
                to = e["to"][0] if isinstance(e["to"], list) else e["to"]
                created = e.get("created_at", "")
                if to.lower() == EMAIL and "logto" in (e.get("subject") or "").lower():
                    req2 = urllib.request.Request(f"https://api.resend.com/emails/{e['id']}",
                                                  headers={"Authorization": f"Bearer {RESEND_KEY}"})
                    full = json.load(urllib.request.urlopen(req2, timeout=10))
                    body = full.get("text") or full.get("html") or ""
                    m = re.search(r"\b(\d{6})\b", body)
                    if m:
                        return m.group(1)
        except Exception as exc:
            print("  resend poll error:", exc)
        time.sleep(4)
    return None


async def main():
    raw = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:9333/json/new?about:blank", method="PUT"))
    page = json.load(raw)
    try:
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**24) as ws:
            mid = 0
            async def call(method, params=None):
                nonlocal mid
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                    if m.get("id") == mid:
                        return m.get("result", {})

            async def ev(expr):
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
                res = r.get("result", {})
                if res.get("subtype") == "error":
                    return "<jserr> " + str(res.get("description", ""))[:150]
                return res.get("value")

            def setv(sel_or_el, v):
                return f"(() => {{ const el = {sel_or_el}; if (!el) return 'no-el'; const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype; const set = Object.getOwnPropertyDescriptor(proto, 'value').set; el.focus(); set.call(el, {json.dumps(v)}); el.dispatchEvent(new Event('input', {{bubbles:true}})); el.dispatchEvent(new Event('change', {{bubbles:true}})); el.blur(); return 'ok'; }})()"

            await call("Page.enable")
            await call("Page.navigate", {"url": "https://staging.resumeforu.com/"})
            await asyncio.sleep(5)
            await ev("location.assign('https://staging.resumeforu.com/signup'); 'nav'")
            await asyncio.sleep(8)
            print("screen:", str(await ev("document.body.innerText.slice(0,60).replace(/[\\n]/g,' | ')")))
            print("email:", await ev(setv("[...document.querySelectorAll('input')].find(i => i.type !== 'checkbox' && i.type !== 'submit')", EMAIL)))
            await asyncio.sleep(0.8)
            print("submit:", await ev("[...document.querySelectorAll('button')].find(b => /create account/i.test(b.textContent))?.click() ?? 'no-btn'"))
            await asyncio.sleep(6)
            print("screen2:", str(await ev("document.body.innerText.slice(0,90).replace(/[\\n]/g,' | ')")))

            print("polling Resend for code...")
            code = await asyncio.get_event_loop().run_in_executor(None, resend_code)
            print("CODE:", code)
            if not code:
                print("ABORT: no code"); return

            filled = await ev(f"(() => {{ const code = {json.dumps(code)}; const digits = [...document.querySelectorAll('input')].filter(i => i.maxLength === 1); if (digits.length >= 6) {{ const set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set; digits.slice(0,6).forEach((el, i) => {{ el.focus(); set.call(el, code[i]); el.dispatchEvent(new Event('input', {{bubbles:true}})); }}); return 'digits:' + digits.length; }} const single = document.querySelector('input[inputmode=numeric], input[autocomplete=one-time-code], input[name*=code i]'); if (single) {{ const set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set; single.focus(); set.call(single, code); single.dispatchEvent(new Event('input', {{bubbles:true}})); return 'single'; }} return 'none: ' + [...document.querySelectorAll('input')].map(i => i.type + '/' + i.maxLength + '/' + (i.name || i.id)).join(','); }})()")
            print("code-fill:", filled)
            await asyncio.sleep(6)
            state = str(await ev("document.body.innerText.slice(0, 250).replace(/[\\n]/g,' | ')"))
            print("screen3:", state)

            if "Continue" in state:
                print("continue:", await ev("[...document.querySelectorAll('button')].find(b => /continue/i.test(b.textContent))?.click() ?? 'no-continue'"))
                await asyncio.sleep(5)
                state = str(await ev("document.body.innerText.slice(0, 250).replace(/[\\n]/g,' | ')"))
                print("screen4:", state)

            # profile form: fill birthdate if present (3 selects or a date input)
            for attempt in range(4):
                print("state:", state[:150])
                if "staging.resumeforu.com" in str(await ev("location.href")) and "/auth/" not in str(await ev("location.href")):
                    break
                # date input
                print("birthdate:", await ev(setv("document.querySelector('input[type=date]')", "1995-06-15")))
                # Logto birthday selects (day/month/year)
                print("bday-selects:", await ev("(() => { const sels = [...document.querySelectorAll('select')]; if (sels.length < 3) return 'selects:' + sels.length; const set = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set; const assign = (el, v) => { const o = [...el.options].find(o => o.value === v || o.label === v); if (o) { set.call(el, o.value); el.dispatchEvent(new Event('change', {bubbles:true})); } }; assign(sels[0], '15'); assign(sels[1], 'June'); assign(sels[2], '1995'); return 'set ' + sels.length; })()"))
                await asyncio.sleep(1)
                name_fill = await ev(setv("[...document.querySelectorAll('input')].find(i => /name/i.test(i.labels?.[0]?.textContent || i.name || i.placeholder || ''))", "Alex Chen"))
                print("name:", name_fill)
                btns = str(await ev("[...document.querySelectorAll('button')].map(b => b.textContent.trim().slice(0,25)).filter(Boolean).join(' ;; ')"))
                print("buttons:", btns)
                clicked = await ev("[...document.querySelectorAll('button')].find(b => /continue|create|agree|next|skip|not now/i.test(b.textContent))?.click() ?? 'nothing-to-click'")
                print("click:", clicked)
                await asyncio.sleep(6)
                state = str(await ev("document.body.innerText.slice(0, 250).replace(/[\\n]/g,' | ')"))
            await asyncio.sleep(8)
            print("FINAL URL:", await ev("location.href"))
            print("FINAL TITLE:", await ev("document.title"))
            print("FINAL BODY:", str(await ev("document.body.innerText.slice(0, 300).replace(/[\\n]/g,' | ')")))
    finally:
        # leave the tab OPEN (signed-in session useful for next steps)
        print("tab kept open:", page["id"])


asyncio.run(main())
