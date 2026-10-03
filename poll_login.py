import asyncio, json, time, urllib.request
import websockets

def targets():
    return json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list"))

# 1) poll up to 150s for the tab to leave /login
page = None
deadline = time.time() + 150
while time.time() < deadline:
    ts = targets()
    cand = [t for t in ts if t["type"] == "page" and "secrets.nodesify.dev" in t.get("url", "")]
    if cand and "/login" not in cand[0]["url"] and cand[0]["url"].rstrip("/") != "https://secrets.nodesify.dev":
        page = cand[0]
        break
    time.sleep(3)

if page is None:
    print("STATUS: still_on_login_after_150s")
    raise SystemExit(0)

print(f"STATUS: logged_in -> {page['url']}")
time.sleep(3)  # let the app settle

# 2) read storage KEY NAMES only (never values)
async def keys():
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**23) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        await ws.recv()
        await ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {
            "expression": "JSON.stringify({ls: Object.keys(localStorage), cookies: document.cookie.split(';').map(c=>c.trim().split('=')[0]).filter(Boolean)})",
            "returnByValue": True}}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if msg.get("id") == 2:
                print("KEYS:", msg["result"]["result"]["value"])
                break

asyncio.run(keys())
