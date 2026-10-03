import asyncio, json, urllib.request
import websockets

ts = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list"))
page = next(t for t in ts if t["type"] == "page" and "nodesify.dev" in t.get("url", ""))
print("URL:", page["url"])

async def main():
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**23) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        await ws.recv()
        await ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {
            "expression": "Promise.all(['/api/v1/user/me','/api/v1/users/me','/api/v2/users/me','/api/v3/users/me','/api/v1/status'].map(p=>fetch(p,{credentials:'include'}).then(r=>p+':'+r.status).catch(e=>p+':ERR'))).then(a=>a.join(' '))",
            "awaitPromise": True, "returnByValue": True}}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if msg.get("id") == 2:
                print(msg["result"]["result"]["value"])
                break

asyncio.run(main())
