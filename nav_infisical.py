import asyncio, json, urllib.request
import websockets

async def main():
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list"))
    page = next((t for t in targets if t["type"] == "page" and t["url"].startswith("about:blank")), None)
    if page is None:
        page = next(t for t in targets if t["type"] == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**23) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
        await ws.recv()
        await ws.send(json.dumps({"id": 2, "method": "Page.navigate",
                                  "params": {"url": "https://sm.nodesify.dev/login"}}))
        for _ in range(5):
            if json.loads(await asyncio.wait_for(ws.recv(), 10)).get("id") == 2:
                break
        await asyncio.sleep(6)

asyncio.run(main())
