import asyncio, json, urllib.request
import websockets

async def main():
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list"))
    page = next((t for t in targets if t["type"] == "page" and t["url"] == "about:blank"), None)
    if page is None:
        req = urllib.request.Request("http://127.0.0.1:9333/json/new", method="PUT")
        page = json.load(urllib.request.urlopen(req))
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**23) as ws:
        async def call(msg_id, method, params=None, timeout=25):
            await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                if m.get("id") == msg_id:
                    return m
        await call(1, "Page.enable")
        await call(2, "Page.navigate", {"url": "http://127.0.0.1:7788/"})
        await asyncio.sleep(7)
        expr = """
(() => {
          const btns = [...document.querySelectorAll('button,[role="tab"],.cds--tabs__nav-link')];
          const s = btns.find(b => /settings/i.test(b.textContent || ''));
          if (s) { s.click(); return 'clicked: ' + (s.textContent||'').trim(); }
          return 'NO SETTINGS TAB; buttons=' + btns.slice(0,15).map(b=>(b.textContent||'').trim()).filter(Boolean).join('|');
        })()
        """
        m = await call(3, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
        print(m["result"]["result"].get("value"))
        await asyncio.sleep(2)
        expr2 = """
(() => {
          const titles = [...document.querySelectorAll('.panel-section-title')].map(t => t.textContent.trim());
          const inputs = [...document.querySelectorAll('input, textarea')].map(i => ({
            label: (i.labels && i.labels[0] ? i.labels[0].textContent.trim() : i.getAttribute('aria-label') || i.id || ''),
            type: i.type,
          }));
          const pw = inputs.filter(i => i.type === 'password').length;
          const credish = inputs.filter(i => /key|token|secret|password|credential/i.test(i.label)).map(i => i.label + ' (' + i.type + ')');
          return JSON.stringify({titles, inputCount: inputs.length, passwordInputs: pw, credishLabeled: credish}, null, 1);
        })()
        """
        m = await call(4, "Runtime.evaluate", {"expression": expr2, "returnByValue": True})
        print(m["result"]["result"].get("value"))
        # also verify the panel authenticated (no 401 banner)
        m = await call(5, "Runtime.evaluate", {
            "expression": "document.body.innerText.includes('token-protected') || document.body.innerText.includes('401') ? 'AUTH ISSUE' : 'panel authenticated OK'",
            "returnByValue": True})
        print(m["result"]["result"].get("value"))

asyncio.run(main())
