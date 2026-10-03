import asyncio, json, sys, urllib.request
import websockets

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.ovh.com/manager/dedicated/#/ip"

async def main():
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list"))
    pages = [t for t in targets if t["type"] == "page"]
    # prefer an ovh-related tab, else first page
    page = next((t for t in pages if "ovh" in t["url"]), pages[0] if pages else None)
    if not page:
        print("no page tab"); return
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**23) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
        await ws.recv()
        await ws.send(json.dumps({"id": 2, "method": "Page.navigate", "params": {"url": URL}}))
        for _ in range(6):
            m = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if m.get("id") == 2: break
        await asyncio.sleep(8)
        await ws.send(json.dumps({"id": 10, "method": "Runtime.evaluate",
                                  "params": {"expression": "document.title + ' ||| ' + location.href"}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if m.get("id") == 10:
                print("PAGE:", m.get("result", {}).get("result", {}).get("value", "?")); break

asyncio.run(main())
