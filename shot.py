import asyncio, base64, json, sys, urllib.request
import websockets

MATCH = sys.argv[1] if len(sys.argv) > 1 else "ovhcloud"
OUT = sys.argv[2] if len(sys.argv) > 2 else "shot.png"

async def main():
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list"))
    pages = [t for t in targets if t["type"] == "page" and MATCH in t["url"]]
    if not pages:
        print("no matching tab"); return
    page = pages[0]
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**24) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot"}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 20))
            if m.get("id") == 1:
                data = m["result"]["data"]
                open(OUT, "wb").write(base64.b64decode(data))
                print("saved", OUT, len(data), "b64 chars")
                break

asyncio.run(main())
