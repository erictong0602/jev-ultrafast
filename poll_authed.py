import asyncio, json, time, urllib.request
import websockets

def get_page():
    ts = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list"))
    return next(t for t in ts if t["type"] == "page" and "nodesify.dev" in t.get("url", ""))

async def probe(page):
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**23) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        await ws.recv()
        await ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {
            "expression": "fetch('/api/v2/users/me', {credentials:'include'}).then(r=>String(r.status)).catch(e=>'ERR:'+e.message)",
            "awaitPromise": True, "returnByValue": True}}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if msg.get("id") == 2:
                return msg["result"]["result"]["value"]

deadline = time.time() + 600
last = None
while time.time() < deadline:
    try:
        status = asyncio.run(probe(get_page()))
        if status != last:
            print(f"[{int(time.time()%100000)}] auth probe: {status}", flush=True)
            last = status
        if status == "200":
            print("AUTHENTICATED")
            break
    except Exception as e:
        print("probe error:", type(e).__name__)
    time.sleep(5)
else:
    print("TIMEOUT_NOT_AUTHED")
