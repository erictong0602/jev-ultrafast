"""Goal-driven walkthroughs with typed outcomes and error evidence.

Single run (unchanged default behavior):
  uv run --env-file .env python examples/run.py --url URL --goal 'A narrow goal'

Batch sweep with per-page JSONL records:
  uv run --env-file .env --frozen python examples/run.py --urls pages.txt \
      --jsonl results.jsonl --goal 'Exercise this page'
"""

import argparse
import json

from jev_ultrafast import Agent
from jev_ultrafast.agent import BudgetExhausted, Stalled
from jev_ultrafast.reporting import build_record, check_expectations

parser = argparse.ArgumentParser()
parser.add_argument("--url")
parser.add_argument("--urls", help="File with one URL per line; blank lines and # comments are skipped.")
parser.add_argument("--goal", action="append", required=True, help="Repeat for an ordered list of goals.")
parser.add_argument("--json", action="store_true", help="Print the JSON record of every run.")
parser.add_argument("--jsonl", help="Append one JSON record per run to this file.")
parser.add_argument("--foreground", action="store_true", help="Open the working tab in the foreground.")
parser.add_argument("--keep-open", action="store_true", help="Leave the tab open after the run ends.")
parser.add_argument("--max-actions", type=int, help="Executed-action budget (default 60).")
parser.add_argument("--max-decisions", type=int, help="Billed-decision budget (default 120).")
parser.add_argument("--max-stall", type=int, help="Stop after N consecutive decisions that execute nothing.")
parser.add_argument("--retries", type=int, help="Retries for transient invalid model responses (default 2).")
parser.add_argument("--expect-url", help="Substring the final URL must contain, else the run fails.")
parser.add_argument("--expect-text", help="Substring the final page text must contain, else the run fails.")
args = parser.parse_args()

if args.urls and args.url:
    parser.error("Use --url or --urls, not both")


def url_list():
    if args.urls:
        with open(args.urls, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#"):
                    yield line
    elif args.url:
        yield args.url
    else:
        parser.error("Supply --url or --urls")


def agent_options():
    names = {"foreground": args.foreground, "keep_open": args.keep_open}
    options = {k: v for k, v in names.items() if v}
    for flag, key in (("--max-actions", "max_actions"), ("--max-decisions", "max_decisions"),
                      ("--max-stall", "max_stall"), ("--retries", "choose_retries")):
        value = getattr(args, flag.lstrip("-").replace("-", "_"))
        if value is not None:
            options[key] = value
    return options


def run_one(url):
    """One walkthrough. Errors are data: any stop becomes a typed record, never a traceback."""
    detail, status, state, errors = "", "error", None, {"counts": {}, "items": []}
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
    except ValueError as error:
        if "TEXT_MODEL_API_KEY" in str(error):
            # Typing without a text key is a supported-operation gap, not a crash: the run hit a
            # fill it cannot legally perform. Same meaning as the model's BLOCKED judgment.
            status, detail = "blocked", str(error)
        else:
            detail = f"{type(error).__name__}: {error}"
    except Exception as error:  # A crashed run still yields a record; no traceback in batch output.
        detail = f"{type(error).__name__}: {error}"
    finally:
        if agent is not None:
            errors = agent.browser.error_summary()
            agent.close()
    page = (state or {}).get("page") or {}
    expectations = None
    if args.expect_url or args.expect_text:
        expectations = check_expectations(page, args.expect_url, args.expect_text)
        if not all(expectations.values()):
            failed = ", ".join(k for k, ok in expectations.items() if not ok)
            status, detail = "failed_expectation", f"expectation mismatch: {failed}"
    return build_record(
        url=url, status=status, detail=detail, page=page,
        actions=len((state or {}).get("history", [])),
        decisions=len((state or {}).get("decisions", [])),
        elapsed_ms=(state or {}).get("elapsed_ms", 0),
        errors=errors, expectations=expectations,
    )


out = open(args.jsonl, "a", encoding="utf-8") if args.jsonl else None
try:
    for target in url_list():
        record = run_one(target)
        line = json.dumps(record)
        if out:
            out.write(line + "\n")
            out.flush()
        if args.json:
            print(line)
        elif args.urls:
            print(f"== {record['status']}  actions={record['actions']}  errors={record['errors']['counts']}  {target}")
finally:
    if out:
        out.close()
