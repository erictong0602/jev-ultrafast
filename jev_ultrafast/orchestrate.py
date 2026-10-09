"""Run several Jev agents concurrently: one task queue, one shared Chrome.

Each task becomes its own `examples/run.py` subprocess with a distinct agent
name, so harness gives every worker its own daemon, tab, and CDP event stream;
results append (locked) to one JSONL with an `agent` field per row. Tasks on
the same site queue on the origin lock instead of fighting over one session.

Task file: one JSON object per line.
  {"url": "https://example.com/", "goals": ["Observe this page, then stop."],
   "expect-text": "Example Domain"}

  jev-orchestrate --tasks tasks.jsonl --jobs 3 --jsonl results.jsonl --origin-wait 120
  jev-orchestrate --tasks tasks.jsonl --dry-run
"""



import argparse
import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path


def find_runner():
    """The examples/run.py that powers workers: JEV_CHECKOUT wins, else the
    nearest checkout at or above the working directory. An installed
    jev-orchestrate has no repo of its own, hence the env escape hatch."""
    env = os.environ.get("JEV_CHECKOUT", "").strip()
    if env:
        candidate = Path(env) / "examples" / "run.py"
        if not candidate.exists():
            raise SystemExit(f"JEV_CHECKOUT={env} has no examples/run.py")
        return candidate
    for base in [Path.cwd(), *Path.cwd().parents]:
        candidate = base / "examples" / "run.py"
        if candidate.exists():
            return candidate
    raise SystemExit(
        "no examples/run.py at or above the working directory; "
        "run from the jev-ultrafast checkout or set JEV_CHECKOUT"
    )

TASK_FIELDS = {
    # task key -> (runner flag, json list vs single)
    "expect-url": ("--expect-url", False),
    "expect-text": ("--expect-text", False),
    "max-actions": ("--max-actions", False),
    "max-decisions": ("--max-decisions", False),
    "max-stall": ("--max-stall", False),
    "retries": ("--retries", False),
    "origin-wait": ("--origin-wait", False),
    "trace": ("--trace", False),
}
TASK_FLAGS = {"no-heal": "--no-heal", "keep-open": "--keep-open", "foreground": "--foreground"}


def parse_tasks(path):
    """One (line number, task dict) per non-blank line; bad lines report, good ones run."""
    tasks = []
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            task = json.loads(stripped)
        except ValueError as error:
            print(f"task line {number}: skipped ({error})", file=sys.stderr)
            continue
        if not task.get("url") or not task.get("goals"):
            print(f"task line {number}: skipped (needs url and goals)", file=sys.stderr)
            continue
        tasks.append((number, task))
    return tasks


def worker_command(runner, task, *, jsonl, origin_wait):
    """The examples/run.py invocation for one task."""
    cmd = [sys.executable, str(runner), "--url", task["url"]]
    for goal in task["goals"]:
        cmd += ["--goal", goal]
    for key, (flag, _) in TASK_FIELDS.items():
        if task.get(key) is not None:
            cmd += [flag, str(task[key])]
    for key, flag in TASK_FLAGS.items():
        if task.get(key):
            cmd += [flag]
    if jsonl:
        cmd += ["--jsonl", str(jsonl)]
    wait = task.get("origin-wait")
    cmd += ["--origin-wait", str(origin_wait if wait is None else wait)]
    if task.get("json"):
        cmd += ["--json"]
    return cmd


def worker_env(name):
    """A distinct agent identity per worker: its own daemon, tab, and event stream."""
    return {**os.environ, "BU_NAME": name, "JEV_AGENT_NAME": name}


def run_queue(tasks, *, jobs, jsonl, origin_wait, dry_run):
    """Feed tasks to N workers; each worker owns one agent name and runs its
    tasks' subprocesses serially. Returns one (line number, returncode) row."""
    runner = find_runner()
    checkout = runner.parent.parent
    pending = queue.Queue()
    for item in tasks:
        pending.put(item)
    rows = []
    rows_lock = threading.Lock()
    counters = itertools.count(1)

    def work():
        name = f"jev-{next(counters)}"
        while True:
            try:
                number, task = pending.get_nowait()
            except queue.Empty:
                return
            cmd = worker_command(runner, task, jsonl=jsonl, origin_wait=origin_wait)
            label = f"[{name} #{number}]"
            if dry_run:
                print(f"{label} BU_NAME={name} {' '.join(cmd)}")
                with rows_lock:
                    rows.append((number, 0))
                continue
            print(f"{label} start {task['url']}", flush=True)
            started = time.monotonic()
            result = subprocess.run(cmd, cwd=checkout, env=worker_env(name))
            print(f"{label} done rc={result.returncode} in {time.monotonic() - started:.1f}s", flush=True)
            with rows_lock:
                rows.append((number, result.returncode))

    threads = [threading.Thread(target=work) for _ in range(min(jobs, len(tasks) or 1))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return rows


def summarize(jsonl):
    """Status tally per agent from the shared results file, workers first."""
    if not jsonl or not Path(jsonl).exists():
        return
    tally = {}
    for line in Path(jsonl).read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        agent = record.get("agent", "?")
        row = tally.setdefault(agent, {})
        row[record.get("status", "?")] = row.get(record.get("status", "?"), 0) + 1
    for agent, statuses in sorted(tally.items()):
        pretty = ", ".join(f"{status}={count}" for status, count in sorted(statuses.items()))
        print(f"== {agent}: {pretty}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks", required=True, help="JSONL task file: url, goals[], options.")
    parser.add_argument("--jobs", type=int, default=2, help="Concurrent agents (default 2).")
    parser.add_argument("--jsonl", help="Shared results file; rows carry the agent name.")
    parser.add_argument("--origin-wait", type=int, default=0,
                        help="Seconds workers wait for a same-origin lock before failing (default 0).")
    parser.add_argument("--dry-run", action="store_true", help="Print planned commands; run nothing.")
    args = parser.parse_args()

    if not Path(args.tasks).exists():
        parser.error(f"tasks file not found: {args.tasks}")
    tasks = parse_tasks(args.tasks)
    if not tasks:
        parser.error(f"no runnable tasks in {args.tasks}")
    print(f"{len(tasks)} task(s), {args.jobs} worker(s)" + (" (dry run)" if args.dry_run else ""))
    rows = run_queue(tasks, jobs=args.jobs, jsonl=args.jsonl,
                     origin_wait=args.origin_wait, dry_run=args.dry_run)
    failed = [number for number, code in rows if code not in (0, None)]
    if failed:
        print(f"non-zero worker exits: task line(s) {', '.join(map(str, failed))}")
    summarize(args.jsonl)


if __name__ == "__main__":
    main()
