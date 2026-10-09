"""Inspect, snapshot, or reset the session Jev's browser tab uses for one origin.

Run only when a site's login misbehaves: the default clears that origin's
cookies and cache, never the rest of the profile. Cookie values never print;
names and counts do. Snapshots hold live session credentials and are stored
under ~/.jev-ultrafast/sessions — never inside a repository. The tabs command
works on the whole browser without opening anything.

  uv run python scripts/session.py status https://example.com
  uv run python scripts/session.py save staging https://example.com   # before risky work
  uv run python scripts/session.py clear https://example.com --cache
  uv run python scripts/session.py load staging https://example.com   # put the login back
  uv run python scripts/session.py clear https://example.com --all  # every origin
  uv run python scripts/session.py tabs
  uv run python scripts/session.py tabs --close https://staging.example.com
"""

import argparse
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from jev_ultrafast.browser import Browser, cdp

SESSIONS_DIR = Path(os.environ.get("JEV_SESSIONS_DIR") or Path.home() / ".jev-ultrafast" / "sessions")

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("command", choices=["status", "clear", "tabs", "save", "load"])
parser.add_argument("name", nargs="?", help="Snapshot name, for save and load.")
parser.add_argument("url", nargs="?", help="Required for status, clear, save, and load.")
parser.add_argument("--cache", action="store_true", help="Also empty the HTTP cache.")
parser.add_argument("--all", action="store_true", help="Clear cookies for every origin, not just this one.")
parser.add_argument("--close", metavar="URL_PREFIX",
                    help="With tabs: close every page whose URL starts with this prefix.")
args = parser.parse_args()

if args.command in {"status", "clear", "save", "load"} and not args.url:
    parser.error(f"{args.command} needs a URL")
if args.command in {"save", "load"} and not args.name:
    parser.error(f"{args.command} needs a snapshot name")


def snapshot_path(name):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return SESSIONS_DIR / f"{safe}.json"


def list_tabs():
    infos = cdp("Target.getTargets").get("targetInfos", [])
    return [t for t in infos if t.get("type") == "page"]


if args.command == "tabs":
    pages = list_tabs()
    for tab in pages:
        print(f"{tab['url'][:90]}  |  {tab['title'][:50]}")
    if args.close:
        closing = [tab for tab in pages if tab["url"].startswith(args.close)]
        for tab in closing:
            cdp("Target.closeTarget", targetId=tab["targetId"])
        print(f"closed {len(closing)} tab(s) matching {args.close!r}")
    raise SystemExit(0)

# Operator override: inspect/clear a site even while an agent works on it.
browser = Browser(args.url, collect_errors=True, heal_session=False, lock_origin=False)
try:
    if args.command == "status":
        pattern = os.environ.get("JEV_LOGIN_URL_PATTERN", "").strip()
        names = sorted(cookie["name"] for cookie in browser.cookies(args.url))
        print(f"document: HTTP {browser.document_status} {browser.document_url or args.url}")
        print(f"cookies for {urlparse(args.url).netloc}: {len(names)}")
        if names:
            print("  " + "\n  ".join(names))
        if pattern:
            print(f"login pattern: {pattern!r} matches={not browser.session_ok(args.url)}")
        print("verdict:", "session looks alive" if browser.session_ok(args.url) else "logged-out signals present")
        if browser.session_events:
            print("healing:", "; ".join(browser.session_events))
    elif args.command == "save":
        count = browser.save_cookies(args.url, snapshot_path(args.name))
        print(f"saved {count} cookie(s) for {urlparse(args.url).netloc} -> {snapshot_path(args.name)}")
    elif args.command == "load":
        count = browser.restore_cookies(snapshot_path(args.name))
        live = len(browser.cookies(args.url))
        print(f"restored {count} cookie(s) from {snapshot_path(args.name)}; {live} now live for "
              f"{urlparse(args.url).netloc}")
    else:
        if args.all:
            print("Clearing cookies for EVERY origin; other sites will forget you.")
        before = browser.cookies(None if args.all else args.url)
        browser.clear_cookies(None if args.all else args.url)
        if args.cache:
            browser.clear_cache()
        after = browser.cookies(None if args.all else args.url)
        scope = "all origins" if args.all else urlparse(args.url).netloc
        print(f"cookies for {scope}: {len(before)} -> {len(after)}" + ("; cache emptied" if args.cache else ""))
finally:
    browser.close()
