import asyncio, json, urllib.request
import websockets

TOKEN = None
for line in open("C:/Nodesify/harness-engineering/.env", encoding="utf-8"):
    if line.startswith("HARNESS_CONTROL_TOKEN="):
        TOKEN = line.split("=", 1)[1].strip()
assert TOKEN

async def main():
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list"))
    page = next(t for t in targets if t["type"] == "page" and t["url"] == "about:blank")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**23) as ws:
        async def call(msg_id, method, params=None, timeout=20):
            await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                if m.get("id") == msg_id:
                    return m
        await call(1, "Page.enable")
        # Inert same-origin page: the 401 JSON error body — no panel JS, no
        # window.confirm, so nothing blocks the evaluate.
        await call(2, "Page.navigate", {"url": "http://127.0.0.1:7788/api/none"})
        await asyncio.sleep(3)
        m = await call(3, "Runtime.evaluate", {
            "expression": "localStorage.setItem('harness-control-token', '%s'); 'seeded:' + localStorage.getItem('harness-control-token').length" % TOKEN,
            "returnByValue": True})
        print("seed:", m["result"]["result"].get("value"))
        await call(4, "Page.navigate", {"url": "about:blank"})
        print("parked")

asyncio.run(main())
