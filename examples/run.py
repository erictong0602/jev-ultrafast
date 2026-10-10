"""Goal-driven walkthroughs with typed outcomes and error evidence.

Single run (unchanged default behavior):
  uv run --env-file .env python examples/run.py --url URL --goal 'A narrow goal'

Batch sweep with per-page JSONL records. A --urls line may carry per-page
expectations, so the same file is the regression suite:
  https://example.com/pricing expect-text=Start free trial
  uv run --env-file .env --frozen python examples/run.py --urls pages.txt \
      --jsonl results.jsonl --goal 'Exercise this page'
"""

import argparse
import json
import re
import time
from pathlib import Path

from jev_ultrafast import Agent
from jev_ultrafast.agent import BudgetExhausted, Stalled
from jev_ultrafast.browser import AGENT_NAME, OriginBusy, cdp
from jev_ultrafast.coordination import append_record
from jev_ultrafast.reporting import (
    build_record,
    check_expectations,
    parse_pages_file,
    record_passed,
    redact_url,
    steps_from_history,
)

parser = argparse.ArgumentParser()
parser.add_argument("--url")
parser.add_argument("--urls", help="File of page specs: URL [expect-url=S] [expect-text=S]; "
                                   "' #' comments and blank lines are skipped.")
parser.add_argument("--goal", action="append", required=True, help="Repeat for an ordered list of goals.")
parser.add_argument("--json", action="store_true", help="Print the JSON record of every run.")
parser.add_argument("--jsonl", help="Append one JSON record per run to this file.")
parser.add_argument("--trace", help="Write the last run's executed steps as an e2e draft JSON "
                                   "(best with a single --url run).")
parser.add_argument("--foreground", action="store_true", help="Open the working tab in the foreground.")
parser.add_argument("--keep-open", action="store_true", help="Leave the tab open after the run ends.")
parser.add_argument("--max-actions", type=int, help="Executed-action budget (default 60).")
parser.add_argument("--max-decisions", type=int, help="Billed-decision budget (default 120).")
parser.add_argument("--max-stall", type=int, help="Stop after N consecutive decisions that execute nothing.")
parser.add_argument("--max-covered", type=int,
                    help="Stop after N consecutive decisions refused because an overlay covers the target (default 6).")
parser.add_argument("--retries", type=int, help="Retries for transient invalid model responses (default 2).")
parser.add_argument("--expect-url", help="Fallback URL substring; a page spec's own expect-url wins.")
parser.add_argument("--expect-text", help="Fallback page-text substring; a page spec's own expect-text wins.")
parser.add_argument("--no-heal", action="store_true",
                    help="Disable session healing for this sweep (never reload or clear storage).")
parser.add_argument("--file", action="append",
                    help="Local file the agent may attach to a file input (repeatable; enables SET_FILE).")
parser.add_argument("--origin-wait", type=int, default=0,
                    help="Seconds to wait for a same-origin lock held by another agent (default 0: fail fast).")
args = parser.parse_args()

if args.urls and args.url:
    parser.error("Use --url or --urls, not both")


def page_specs():
    """(url, expect_url, expect_text) per target; file specs override the CLI fallbacks."""
    if args.urls:
        for url, expect_url, expect_text in parse_pages_file(args.urls):
            yield url, expect_url or args.expect_url, expect_text or args.expect_text
    elif args.url:
        yield args.url, args.expect_url, args.expect_text
    else:
        parser.error("Supply --url or --urls")


def agent_options():
    names = {"foreground": args.foreground, "keep_open": args.keep_open,
             "heal_session": not args.no_heal}
    options = {k: v for k, v in names.items() if v}
    options["origin_wait"] = args.origin_wait
    if args.file:
        options["files"] = args.file
    for flag, key in (("--max-actions", "max_actions"), ("--max-decisions", "max_decisions"),
                      ("--max-stall", "max_stall"), ("--max-covered", "max_covered"),
                      ("--retries", "choose_retries")):
        value = getattr(args, flag.lstrip("-").replace("-", "_"))
        if value is not None:
            options[key] = value
    return options


def failure_evidence(agent, url, status, expectations):
    """One JPEG of the final page when a run fails; humans read it, the model never does."""
    if agent is None:
        return None
    if status in {"done", "blocked"} and (expectations is None or all(expectations.values())):
        return None
    session = None
    try:
        import base64

        # After a finished loop the run's own CDP session can stop answering
        # captureScreenshot (evaluates still work). A fresh attach to the same
        # tab always captures, so evidence borrows one and lets it go.
        agent.browser._drain()
        session = cdp("Target.attachToTarget", targetId=agent.browser.target, flatten=True)["sessionId"]
        shot = cdp("Page.captureScreenshot", session_id=session, _response_timeout=30,
                   format="jpeg", quality=72)
        folder = Path("artifacts/sweep-failures")
        folder.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", redact_url(url).split("://", 1)[-1])[:80]
        path = folder / f"{int(time.time())}-{slug}.jpg"
        path.write_bytes(base64.b64decode(shot["data"]))
        return str(path)
    except Exception as error:
        print(f"   (failure screenshot unavailable: {type(error).__name__}: {error})")
        return None
    finally:
        if session:
            try:
                cdp("Target.detachFromTarget", sessionId=session)
            except Exception:
                pass


def run_one(url, expect_url, expect_text):
    """One walkthrough. Errors are data: any stop becomes a typed record, never a traceback."""
    detail, status, state, errors = "", "error", None, {"counts": {}, "items": []}
    heal_events = []
    agent = None
    try:
        agent = Agent(url, args.goal, **agent_options())
        for state in agent.run():
            print(f"{state['elapsed_ms']:>5} ms  {len(state['history'])} actions  {state['status']}")
        print(state["page"]["url"])
        status = state["status"]
    except (BudgetExhausted, Stalled) as error:
        status = "budget_exhausted" if isinstance(error, BudgetExhausted) else "stalled"
        detail = str(error)
    except OriginBusy as error:
        status, detail = "origin_busy", str(error)
    except ValueError as error:
        if "TEXT_MODEL_API_KEY" in str(error):
            # Typing without a text key is a supported-operation gap, not a crash: the run hit a
            # fill it cannot legally perform. Same meaning as the model's BLOCKED judgment.
            status, detail = "blocked", str(error)
        else:
            detail = f"{type(error).__name__}: {error}"
    except TimeoutError as error:
        # A wedged renderer or a dead daemon is its own outcome class in long runs.
        status, detail = "timeout", f"{type(error).__name__}: {error}"
    except Exception as error:  # A crashed run still yields a record; no traceback in batch output.
        detail = f"{type(error).__name__}: {error}"
    if agent is not None:
        # Everything between the try block and here is pure computation, so closing
        # after evidence capture loses nothing.
        errors = agent.browser.error_summary()
        heal_events = list(agent.browser.session_events)
    page = (state or {}).get("page") or {}
    expectations = None
    if expect_url or expect_text:
        expectations = check_expectations(page, expect_url, expect_text)
        if not all(expectations.values()):
            failed = ", ".join(k for k, ok in expectations.items() if not ok)
            status, detail = "failed_expectation", f"expectation mismatch: {failed}"
    record = build_record(
        url=url, status=status, detail=detail, page=page,
        actions=len((state or {}).get("history", [])),
        decisions=len((state or {}).get("decisions", [])),
        elapsed_ms=(state or {}).get("elapsed_ms", 0),
        errors=errors, expectations=expectations, heal_events=heal_events,
        refusals=(state or {}).get("refusals"),
        agent=AGENT_NAME,
    )
    record["ts"] = int(time.time())
    screenshot = failure_evidence(agent, url, status, expectations)
    if screenshot:
        record["failure_screenshot"] = screenshot
    if agent is not None:
        if args.trace and state is not None:
            trace = {
                "url": url,
                "goal": args.goal[-1],
                "final_url": page.get("url"),
                "status": status,
                "steps": steps_from_history(state.get("history", [])),
            }
            Path(args.trace).parent.mkdir(parents=True, exist_ok=True)
            Path(args.trace).write_text(json.dumps(trace, indent=2), encoding="utf-8")
        agent.close()
    return record


records = []
for target, expect_url, expect_text in page_specs():
    record = run_one(target, expect_url, expect_text)
    records.append(record)
    line = json.dumps(record)
    if args.jsonl:
        # Locked append: concurrent agents may share one results file.
        append_record(args.jsonl, record)
    if args.json:
        print(line)
    elif args.urls:
        print(f"== {record['status']}  actions={record['actions']}  errors={record['errors']['counts']}  {target}")
if args.urls and records:
    bad = [r["url"] for r in records if not record_passed(r)]
    verdict = f"{len(records) - len(bad)}/{len(records)} pages verified"
    print(f"== {verdict}" + (f"; failing: {', '.join(bad)}" if bad else ""))
