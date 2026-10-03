import asyncio, json, sys, urllib.request
import websockets

async def main():
    x = float(sys.argv[1]); y = float(sys.argv[2])
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list"))
    pages = [t for t in targets if t["type"] == "page" and "manager.ca.ovhcloud.com" in t["url"]]
    page = pages[0]
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**23) as ws:
        for etype in ["mousePressed", "mouseReleased"]:
            await ws.send(json.dumps({"id": 1, "method": "Input.dispatchMouseEvent",
                "params": {"type": etype, "x": x, "y": y, "button": "left", "clickCount": 1}}))
            await asyncio.wait_for(ws.recv(), 10)
    print("clicked", x, y)

asyncio.run(main())
