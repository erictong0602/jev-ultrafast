"""Launch a dedicated automation Chrome with a persistent profile.

The profile lives on disk, so a sign-in survives restarts like a normal
browser, and a dedicated user-data-dir avoids the per-connection
"Allow remote debugging" popup Chrome 144+ shows on the daily profile.

  uv run python scripts/automation_chrome.py
  export BU_CDP_WS=<the printed ws:// URL>
  uv run jev
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = int(os.environ.get("JEV_DEBUG_PORT") or 9333)
PROFILE = Path(os.environ.get("JEV_PROFILE_DIR") or Path.home() / ".jev-ultrafast" / "chrome-profile")


def chrome_binary():
    if raw := (os.environ.get("JEV_CHROME_PATH") or os.environ.get("CHROME_PATH") or "").strip():
        if Path(raw).expanduser().is_file():
            return Path(raw).expanduser()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        candidates = [
            local / "Google/Chrome/Application/chrome.exe",
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    else:
        candidates = [Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium")]
    return next((path for path in candidates if path.is_file()), None)


def endpoint_ready():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2).read()
        return True
    except Exception:
        return False


def devtools_ws():
    try:
        lines = (PROFILE / "DevToolsActivePort").read_text(encoding="utf-8", errors="replace").splitlines()
        return f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
    except (OSError, IndexError):
        return None


def main():
    ws = devtools_ws()
    if ws is None and endpoint_ready():
        try:
            body = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2).read())
            ws = body.get("webSocketDebuggerUrl")
        except Exception:
            ws = None
    if ws:
        print(f"Chrome is already serving this profile on port {PORT}.")
        print(f"BU_CDP_WS={ws}")
        return

    binary = chrome_binary()
    if not binary:
        raise SystemExit("Chrome not found; set JEV_CHROME_PATH to the chrome executable.")
    PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            str(binary),
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if ws := devtools_ws():
            print(f"Dedicated automation Chrome is running with profile {PROFILE}.")
            print(f"BU_CDP_WS={ws}")
            return
        time.sleep(0.5)
    raise SystemExit(f"Chrome did not open its DevTools port within 30s; is port {PORT} free?")


if __name__ == "__main__":
    main()
