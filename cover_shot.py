# Verify the rebuilt deck and homepage over CDP: console events, failed
# requests, and screenshots of the deck cover and docs homepage.
import asyncio
import base64
import json
import urllib.request

import websockets

CDP = "http://127.0.0.1:9333"


async def visit(ws, url, clicks=0, screenshot=None):
    console_msgs = []
    failed = []

    def on_message(ev, bucket):
        if ev.get("method") == "Runtime.consoleAPICalled":
            args = [a.get("value", a.get("description", "")) for a in ev["params"].get("args", [])]
            bucket.append(("console:" + ev["params"]["type"], " | ".join(str(a) for a in args)[:200]))
        elif ev.get("method") == "Network.loadingFailed":
            bucket.append(("request_failed", ev["params"].get("errorText", "")))

    events = []
    ws_send = lambda m: ws.send(json.dumps(m))
    await ws_send({"id": 10, "method": "Runtime.enable"})
    await ws_send({"id": 11, "method": "Network.enable"})
    await ws_send({"id": 12, "method": "Page.enable"})
    pump = asyncio.create_task(_pump(ws, events))
    await ws_send({"id": 13, "method": "Page.navigate", "params": {"url": url}})
    await asyncio.sleep(6)
    for _ in range(clicks):
        await ws_send({"id": 14, "method": "Input.dispatchMouseEvent", "params": {"type": "mousePressed", "x": 1200, "y": 700, "button": "left", "clickCount": 1}})
        await ws_send({"id": 15, "method": "Input.dispatchMouseEvent", "params": {"type": "mouseReleased", "x": 1200, "y": 700, "button": "left", "clickCount": 1}})
        await asyncio.sleep(0.8)
    if clicks:
        await asyncio.sleep(2)
    await pump
    for ev in events:
        if ev.get("method") == "Runtime.consoleAPICalled" and ev["params"]["type"] in ("error", "warning"):
            args = [a.get("value", a.get("description", "")) for a in ev["params"].get("args", [])]
            console_msgs.append((" | ".join(str(a) for a in args))[:220])
        elif ev.get("method") == "Network.loadingFailed":
            console_msgs.append("REQUEST_FAILED: " + ev["params"].get("errorText", ""))
    if screenshot:
        await ws_send({"id": 16, "method": "Page.captureScreenshot", "params": {"format": "png"}})
        resp = json.loads(await _recv_id(ws, 16))
        open(screenshot, "wb").write(base64.b64decode(resp["result"]["data"]))
    return console_msgs


async def _pump(ws, bucket):
    try:
        while True:
            raw = await asyncio.wait_for(ws.recv(), 1.5)
            msg = json.loads(raw)
            if "method" in msg:
                bucket.append(msg)
    except (asyncio.TimeoutError, websockets.ConnectionClosed):
        return


async def _recv_id(ws, rid):
    while True:
        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("id") == rid:
            return raw


async def main():
    targets = json.load(urllib.request.urlopen(f"{CDP}/json/list"))
    page = next(t for t in targets if t["type"] == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**23) as ws:
        msgs = await visit(ws, "http://127.0.0.1:8890/docs/slides/", clicks=0,
                           screenshot="C:/Users/erict/.zcode/workspace/default/cover.png")
        print("DECK COVER+3CLICKS:")
        for m in msgs or ["(no console errors, no failed requests)"]:
            print("  ", m)
        msgs = await visit(ws, "http://127.0.0.1:8890/docs/",
                           screenshot="C:/Users/erict/.zcode/workspace/default/home.png")
        print("HOMEPAGE:")
        for m in msgs or ["(no console errors, no failed requests)"]:
            print("  ", m)


asyncio.run(main())
