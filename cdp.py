import asyncio, json, sys, urllib.request
import websockets

async def main():
    js = sys.argv[1]
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list"))
    pages = [t for t in targets if t["type"] == "page" and "manager.ca.ovhcloud.com" in t["url"]]
    if not pages:
        print("no manager tab"); return
    async with websockets.connect(pages[0]["webSocketDebuggerUrl"], max_size=2**24) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        await ws.recv()
        await ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                                  "params": {"expression": js, "returnByValue": True, "awaitPromise": True}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if m.get("id") == 2:
                r = m.get("result", {}).get("result", {})
                print(json.dumps(r.get("value", r.get("description", "")), ensure_ascii=False)[:2500])
                break

asyncio.run(main())
