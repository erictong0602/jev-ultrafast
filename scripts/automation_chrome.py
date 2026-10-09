"""Launch a dedicated automation Chrome with a persistent profile.

The profile lives on disk, so a sign-in survives restarts like a normal
browser, and a dedicated user-data-dir avoids the per-connection
"Allow remote debugging" popup Chrome 144+ shows on the daily profile.

One profile per project keeps test accounts and sessions apart. One unique
BU_NAME per session keeps concurrent jev runs from sharing a browser:
daemons are keyed by BU_NAME and each daemon serves exactly one Chrome,
so a second session that changes only the endpoint would silently reuse
the first session's browser (jev fails loudly on that since this guard
exists — Browser checks the live daemon against the requested endpoint).

  uv run python scripts/automation_chrome.py                    # default profile
  uv run python scripts/automation_chrome.py --profile staging  # ~/.jev-ultrafast/profiles/staging
  uv run python scripts/automation_chrome.py --list
  export BU_NAME=staging                     # unique per parallel session; pairs with --profile
  export BU_CDP_URL=http://127.0.0.1:9334    # printed below; stable across Chrome restarts
  uv run jev
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("JEV_HOME") or Path.home() / ".jev-ultrafast")
PORT = int(os.environ.get("JEV_DEBUG_PORT") or 9333)
PROFILE = Path(os.environ.get("JEV_PROFILE_DIR") or (ROOT / "chrome-profile"))

parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
parser.add_argument("--profile", help="Named profile under ~/.jev-ultrafast/profiles; one Chrome each.")
parser.add_argument("--list", action="store_true", help="List known profiles and their ports.")
args = parser.parse_args()


def safe_name(value):
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in value)


def profile_dir():
    if args.profile:
        return ROOT / "profiles" / safe_name(args.profile)
    return PROFILE


def print_exports(port):
    """The two env vars a session needs; both or neither, or isolation is not real."""
    print(f"export BU_CDP_URL=http://127.0.0.1:{port}")
    if args.profile:
        print(f"export BU_NAME={safe_name(args.profile)}")
    else:
        print("# Parallel sessions: pass --profile <name> to also get a unique BU_NAME for this browser.")


def port_for(directory):
    """First launch picks the next free port from 9333 up and keeps it for this profile."""
    marker = directory / "port.txt"
    if marker.is_file():
        try:
            return int(marker.read_text().strip())
        except ValueError:
            pass
    used = set()
    for other in (ROOT / "profiles").glob("*/port.txt"):
        try:
            used.add(int(other.read_text().strip()))
        except (OSError, ValueError):
            continue
    port = PORT
    while port in used and port < PORT + 200:
        port += 1
    directory.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{port}\n", encoding="utf-8")
    return port


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


def endpoint_ready(port):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read()
        return True
    except Exception:
        return False


def devtools_ws(directory, port):
    try:
        lines = (directory / "DevToolsActivePort").read_text(encoding="utf-8", errors="replace").splitlines()
        return f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
    except (OSError, IndexError):
        return None


def serving_port(ws, fallback):
    """The port DevTools actually answered on, when known."""
    try:
        return int(ws.split(":", 3)[2].split("/", 1)[0])
    except (IndexError, ValueError):
        return fallback


def main():
    if args.list:
        default = PROFILE
        print(f"default  {default}  port {PORT}")
        for folder in sorted((ROOT / "profiles").glob("*/port.txt")):
            port = folder.read_text().strip()
            print(f"{folder.parent.name}  {folder.parent}  port {port}")
        return

    directory = profile_dir()
    port = port_for(directory) if args.profile else PORT
    ws = devtools_ws(directory, port)
    if ws is None and endpoint_ready(port):
        try:
            body = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read())
            ws = body.get("webSocketDebuggerUrl")
        except Exception:
            ws = None
    if ws:
        live = serving_port(ws, port)
        print(f"Chrome is already serving {directory} on port {live}.")
        print_exports(live)
        return

    binary = chrome_binary()
    if not binary:
        raise SystemExit("Chrome not found; set JEV_CHROME_PATH to the chrome executable.")
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            str(binary),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={directory}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if ws := devtools_ws(directory, port):
            print(f"Dedicated automation Chrome is running with profile {directory} on port {port}.")
            print_exports(port)
            return
        time.sleep(0.5)
    raise SystemExit(f"Chrome did not open its DevTools port within 30s; is port {port} free?")


if __name__ == "__main__":
    main()
