import asyncio, json, urllib.request
import websockets

TARGET_URL = "https://secrets.nodesify.dev"

async def main():
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list"))
    page = next((t for t in targets if t["type"] == "page" and t["url"].startswith("about:blank")), None)
    if page is None:
        page = next(t for t in targets if t["type"] == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**23) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
        await ws.recv()
        await ws.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
        await ws.recv()
        await ws.send(json.dumps({"id": 3, "method": "Page.navigate", "params": {"url": TARGET_URL}}))
        for _ in range(8):
            if json.loads(await asyncio.wait_for(ws.recv(), 15)).get("id") == 3:
                break
        await asyncio.sleep(10)  # SPA boot settle
        await ws.send(json.dumps({
            "id": 4, "method": "Runtime.evaluate",
            "params": {"expression": "document.body.innerText.slice(0, 600)", "returnByValue": True},
        }))
        for _ in range(10):
            msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if msg.get("id") == 4:
                print("TEXT:", msg["result"]["result"].get("value", "<no text>"))
                break

asyncio.run(main())
