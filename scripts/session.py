"""Inspect or reset the session Jev's browser tab uses for one origin.

Run only when a site's login misbehaves: the default clears that origin's
cookies and cache, never the rest of the profile. Cookie values never print;
names and counts do. The tabs command works on the whole browser without
opening anything — use it to sweep up tabs a crashed run left behind.

  uv run python scripts/session.py status https://example.com
  uv run python scripts/session.py clear https://example.com --cache
  uv run python scripts/session.py clear https://example.com --all  # every origin
  uv run python scripts/session.py tabs
  uv run python scripts/session.py tabs --close https://staging.example.com
"""

import argparse
import os
from urllib.parse import urlparse

from jev_ultrafast.browser import Browser, cdp

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("command", choices=["status", "clear", "tabs"])
parser.add_argument("url", nargs="?", help="Required for status and clear.")
parser.add_argument("--cache", action="store_true", help="Also empty the HTTP cache.")
parser.add_argument("--all", action="store_true", help="Clear cookies for every origin, not just this one.")
parser.add_argument("--close", metavar="URL_PREFIX",
                    help="With tabs: close every page whose URL starts with this prefix.")
args = parser.parse_args()

if args.command in {"status", "clear"} and not args.url:
    parser.error(f"{args.command} needs a URL")


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

browser = Browser(args.url, collect_errors=True, heal_session=False)
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
