"""Deterministic Stripe Checkout filler over raw CDP (fallback if the Jev
loop can't drive checkout.stripe.com).

Finds the checkout.stripe.com tab, fills the classic test card, clicks Pay.
Usage: uv run --frozen python stripe-fill.py [cardNumber expiry cvc]
"""
import asyncio
import json
import sys
import urllib.request

import websockets

CDP_HTTP = "http://127.0.0.1:9333"


async def main():
    card = sys.argv[1] if len(sys.argv) > 1 else "4242424242424242"
    expiry = sys.argv[2] if len(sys.argv) > 2 else "12 / 34"
    cvc = sys.argv[3] if len(sys.argv) > 3 else "123"

    targets = json.load(urllib.request.urlopen(f"{CDP_HTTP}/json/list"))
    tab = next((t for t in targets if t["type"] == "page" and "checkout.stripe.com" in t["url"]), None)
    if not tab:
        print("no checkout.stripe.com tab open"); return
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=2**23) as ws:
        mid = 0
        async def call(method, params=None):
            nonlocal mid
            mid += 1
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if msg.get("id") == mid:
                    return msg.get("result", {})

        async def fill(selector, value):
            # focus + set value via native setter, then fire input events so Stripe's JS notices
            expr = f"""(() => {{
              const el = document.querySelector('{selector}');
              if (!el) return 'missing';
              el.focus();
              const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
              setter.call(el, '{value}');
              el.dispatchEvent(new Event('input', {{bubbles: true}}));
              el.dispatchEvent(new Event('change', {{bubbles: true}}));
              el.blur();
              return 'ok:' + el.value;
            }})()"""
            r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            print(f"  {selector}: {r.get('result', {}).get('value')}")
            await asyncio.sleep(0.6)

        await call("Page.enable")
        print("url:", tab["url"][:80])
        await fill("#cardNumber", card)
        await fill("#cardExpiry", expiry)
        await fill("#cardCvc", cvc)
        # billing fields render after card entry
        r = await call("Runtime.evaluate", {"expression": "!!document.querySelector('#billingName')", "returnByValue": True})
        if r.get("result", {}).get("value"):
            await fill("#billingName", "Alex Chen")
            country = await call("Runtime.evaluate", {"expression": "!!document.querySelector('#billingCountry')", "returnByValue": True})
            if country.get("result", {}).get("value"):
                expr = """(() => {
                  const el = document.querySelector('#billingCountry');
                  const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
                  const opt = [...el.options].find(o => o.label === 'United States' || o.value === 'US');
                  setter.call(el, opt ? opt.value : el.value);
                  el.dispatchEvent(new Event('change', {bubbles: true}));
                  return 'ok:' + el.value;
                })()"""
                await call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                print("  billingCountry: set")
                await asyncio.sleep(0.5)
            await fill("#billingPostalCode", "94103")
        r = await call("Runtime.evaluate", {"expression": """
          (() => { const b = [...document.querySelectorAll('button[type=submit]')].find(b => /pay/i.test(b.textContent)); if (b) { b.click(); return 'clicked: ' + b.textContent.trim(); } return 'no pay button'; })()
        """, "returnByValue": True})
        print("pay:", r.get("result", {}).get("value"))
        await asyncio.sleep(8)
        tabs = json.load(urllib.request.urlopen(f"{CDP_HTTP}/json/list"))
        print("tabs now:", [t["url"][:90] for t in tabs if t["type"] == "page"])


asyncio.run(main())
