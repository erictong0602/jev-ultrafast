"""CDP utilities against the dedicated automation Chrome (port 9333).

Usage (run from the jev-ultrafast checkout):
  uv run --frozen python cdputil.py nav <url> [wait_ms]
  uv run --frozen python cdputil.py shot <url> <out.png> [wait_ms]
  uv run --frozen python cdputil.py tabs
  uv run --frozen python cdputil.py close <targetId>

`nav`/`shot` auto-prefix staging.resumeforu.com with HTTP basic-auth
credentials (from JEV_BASIC_AUTH_* env or --user) so the session caches
them; top-level navigation with userinfo unlocks the whole origin.
"""
import asyncio
import base64
import json
import sys
import urllib.request
import urllib.parse

import websockets

CDP_HTTP = "http://127.0.0.1:9333"


def _creds_for(url: str) -> str | None:
    host = urllib.parse.urlparse(url).netloc
    if "staging.resumeforu.com" not in host:
        return None
    import os
    user = os.environ.get("JEV_BASIC_AUTH_USERNAME", "nodesify")
    pwd = os.environ.get("JEV_BASIC_AUTH_PASSWORD", "")
    return f"{user}:{pwd}@"


def _with_creds(url: str) -> str:
    creds = _creds_for(url)
    if not creds:
        return url
    return url.replace("https://", f"https://{creds}", 1)


async def _page_ws():
    # always create a fresh tab of our own (never touch the harness's tabs)
    raw = urllib.request.urlopen(
        urllib.request.Request(f"{CDP_HTTP}/json/new?about:blank", method="PUT")
    )
    page = json.load(raw)
    return page


async def _nav_and_wait(ws, url, wait_ms):
    await ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
    await ws.send(json.dumps({"id": 2, "method": "Emulation.setDeviceMetricsOverride",
                              "params": {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False}}))
    await ws.send(json.dumps({"id": 3, "method": "Page.navigate", "params": {"url": url}}))
    # drain until we see the navigate result and a load event (or timeout)
    got_result = got_load = False
    deadline = asyncio.get_event_loop().time() + 30
    while asyncio.get_event_loop().time() < deadline and not (got_result and got_load):
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        except asyncio.TimeoutError:
            break
        if msg.get("id") == 3:
            got_result = True
            if "errorText" in msg.get("result", {}):
                print("NAV ERROR:", msg["result"])
        if msg.get("method") == "Page.loadEventFired":
            got_load = True
    await asyncio.sleep(wait_ms / 1000)


async def do_nav(url, wait_ms=4000):
    page = await _page_ws()
    try:
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**23) as ws:
            await _nav_and_wait(ws, _with_creds(url), wait_ms)
            state = json.load(urllib.request.urlopen(f"{CDP_HTTP}/json/list"))
            final = next((t["url"] for t in state if t["id"] == page["id"]), "?")
            print(f"nav: {final}")
    finally:
        try:
            urllib.request.urlopen(f"{CDP_HTTP}/json/close/{page['id']}")
        except Exception:
            pass


async def do_shot(url, out, wait_ms=5000):
    page = await _page_ws()
    try:
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**23) as ws:
            await _nav_and_wait(ws, _with_creds(url), wait_ms)
            # full-page capture: measure document height, resize viewport to it, shoot
            await ws.send(json.dumps({"id": 10, "method": "Runtime.evaluate",
                                      "params": {"expression": "JSON.stringify({h: document.documentElement.scrollHeight, w: document.documentElement.scrollWidth, t: document.title})",
                                                 "returnByValue": True}}))
            dims = None
            for _ in range(50):
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if msg.get("id") == 10:
                    dims = json.loads(msg["result"]["result"]["value"])
                    break
            if not dims:
                print("shot: could not measure page"); return
            h = min(max(dims["h"], 900), 20000)
            await ws.send(json.dumps({"id": 11, "method": "Emulation.setDeviceMetricsOverride",
                                      "params": {"width": 1280, "height": h, "deviceScaleFactor": 1, "mobile": False}}))
            await asyncio.sleep(1.2)
            await ws.send(json.dumps({"id": 12, "method": "Page.captureScreenshot",
                                      "params": {"format": "png", "captureBeyondViewport": True}}))
            for _ in range(50):
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if msg.get("id") == 12:
                    data = base64.b64decode(msg["result"]["data"])
                    with open(out, "wb") as f:
                        f.write(data)
                    print(f"shot: {out} ({dims['w']}x{dims['h']}) title={dims['t']!r}")
                    break
    finally:
        try:
            urllib.request.urlopen(f"{CDP_HTTP}/json/close/{page['id']}")
        except Exception:
            pass


JS_HELPERS = """
window.__setInput = (sel, value) => {
  const el = document.querySelector(sel);
  if (!el) return 'missing: ' + sel;
  el.focus();
  const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, value);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  el.blur();
  return 'ok: ' + sel + ' = ' + String(value).slice(0, 40);
};
window.__click = (sel) => {
  const el = document.querySelector(sel);
  if (!el) return 'missing: ' + sel;
  el.click();
  return 'clicked: ' + sel;
};
window.__clickText = (text) => {
  const el = [...document.querySelectorAll('button, a, [role=button]')].find(b => b.textContent.trim().toLowerCase().includes(text.toLowerCase()));
  if (!el) return 'missing text: ' + text;
  el.click();
  return 'clicked: ' + el.textContent.trim().slice(0, 40);
};
window.__text = (sel) => document.querySelector(sel)?.textContent?.trim() ?? 'missing: ' + sel;
window.__bodyText = () => document.body.innerText.slice(0, 400);
"""


async def do_flow(ops):
    page = await _page_ws()
    try:
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**23) as ws:
            mid = 0
            async def call(method, params=None):
                nonlocal mid
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                    if msg.get("id") == mid:
                        if "error" in msg:
                            print("  ERR", msg["error"].get("message", "")[:120])
                            return None
                        return msg.get("result", {})

            async def evaluate(expr):
                if "__" in expr:
                    await call("Runtime.evaluate", {"expression": JS_HELPERS})
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
                return r.get("result", {}).get("value") if r else "<eval-error>"

            await call("Page.enable")
            for op in ops:
                if "url" in op:
                    target = op["url"]
                    await _nav_and_wait(ws, _with_creds(target), max(op.get("wait", 4000) // 2, 2500))
                    if _creds_for(target):
                        clean = target.replace("https://nodesify:", "https://x:").split("@", 1)
                        # rebuild clean URL: scheme://host/path (drop userinfo)
                        parts = target.split("://", 1)
                        rest = parts[1].split("@", 1)[1] if "@" in parts[1] else parts[1]
                        clean = f"{parts[0]}://{rest}"
                        await call("Runtime.evaluate", {"expression": f"location.replace({json.dumps(clean)})"})
                        await asyncio.sleep(max(op.get("wait", 4000) / 1000, 4))
                    await call("Runtime.evaluate", {"expression": JS_HELPERS})
                    print(f"  nav: {target[:80]}")
                elif "fill" in op:
                    v = str(op["value"]).replace("\\", "\\\\").replace("'", "\\'")
                    print("  ", await evaluate(f"__setInput('{op['fill']}', '{v}')"))
                elif "click" in op:
                    print("  ", await evaluate(f"__click('{op['click']}')"))
                elif "clickText" in op:
                    t = str(op["clickText"]).replace("'", "\\'")
                    print("  ", await evaluate(f"__clickText('{t}')"))
                elif "text" in op:
                    print(f"   text[{op['text'][:30]}]:", await evaluate(f"__text('{op['text']}')"))
                elif "body" in op:
                    print("   body:", str(await evaluate("__bodyText()"))[:250].replace(chr(10), " | "))
                elif "js" in op:
                    print("   js:", await evaluate(op["js"]))
                elif "wait" in op:
                    await asyncio.sleep(op["wait"] / 1000)
                elif "shot" in op:
                    dims_raw = await evaluate("JSON.stringify({h: document.documentElement.scrollHeight})")
                    try:
                        h = min(max(json.loads(dims_raw)["h"], 900), 20000)
                    except Exception:
                        h = 900
                    await call("Emulation.setDeviceMetricsOverride",
                               {"width": 1280, "height": h, "deviceScaleFactor": 1, "mobile": False})
                    await asyncio.sleep(1.2)
                    r = await call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
                    if r and "data" in r:
                        with open(op["shot"], "wb") as f:
                            f.write(base64.b64decode(r["data"]))
                        print(f"   shot: {op['shot']} h={h}")
                    await call("Emulation.setDeviceMetricsOverride",
                               {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False})
            # inject helpers once at start too
    finally:
        # keep-alive: inject helpers after load for subsequent flows via same tab? tab is closed each run
        try:
            urllib.request.urlopen(f"{CDP_HTTP}/json/close/{page['id']}")
        except Exception:
            pass


def main():
    cmd = sys.argv[1]
    if cmd == "nav":
        asyncio.run(do_nav(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 4000))
    elif cmd == "shot":
        asyncio.run(do_shot(sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 5000))
    elif cmd == "flow":
        ops = json.load(open(sys.argv[2], encoding="utf-8"))
        asyncio.run(do_flow(ops))
    elif cmd == "tabs":
        for t in json.load(urllib.request.urlopen(f"{CDP_HTTP}/json/list")):
            if t["type"] == "page":
                print(t["id"], "|", t["title"][:60], "|", t["url"][:100])
    elif cmd == "close":
        print(urllib.request.urlopen(f"{CDP_HTTP}/json/close/{sys.argv[2]}").read().decode())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
