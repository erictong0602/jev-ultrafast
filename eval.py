import asyncio, json, sys, urllib.request
import websockets

EXPR = sys.argv[1] if len(sys.argv) > 1 else "document.body.innerText.slice(0, 4000)"
MATCH = sys.argv[2] if len(sys.argv) > 2 else "ovhcloud"

async def main():
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list"))
    pages = [t for t in targets if t["type"] == "page" and MATCH in t["url"]]
    if not pages:
        print("no matching tab"); return
    page = pages[0]
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**23) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        await ws.recv()
        await ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                                  "params": {"expression": EXPR, "returnByValue": True}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 20))
            if m.get("id") == 2:
                print(m.get("result", {}).get("result", {}).get("value", "no value"))
                break

asyncio.run(main())
