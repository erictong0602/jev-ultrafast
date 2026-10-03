"""Inspect or reset the session Jev's browser tab uses for one origin.

Run only when a site's login misbehaves: the default clears that origin's
cookies and cache, never the rest of the profile. Cookie values never print;
names and counts do.

  uv run python scripts/session.py status https://example.com
  uv run python scripts/session.py clear https://example.com --cache
  uv run python scripts/session.py clear https://example.com --all  # every origin
"""

import argparse
import os
from urllib.parse import urlparse

from jev_ultrafast.browser import Browser

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("command", choices=["status", "clear"])
parser.add_argument("url")
parser.add_argument("--cache", action="store_true", help="Also empty the HTTP cache.")
parser.add_argument("--all", action="store_true", help="Clear cookies for every origin, not just this one.")
args = parser.parse_args()

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
