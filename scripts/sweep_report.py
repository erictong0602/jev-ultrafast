"""Trend evidence over appended sweep JSONL files: per-URL pass rates, flakiest first.

  uv run python scripts/sweep_report.py staging-sweep.jsonl
  uv run python scripts/sweep_report.py runs/oct.jsonl runs/nov.jsonl
"""

import argparse
import json
import time
from datetime import datetime

from jev_ultrafast.reporting import summarize_records

parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
parser.add_argument("jsonl", nargs="+", help="One or more JSONL files of sweep records.")
parser.add_argument("--all", action="store_true", help="Print every URL, not just imperfect ones.")
args = parser.parse_args()

records = []
for path in args.jsonl:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))

if not records:
    raise SystemExit("No records found.")

summary = summarize_records(records)
perfect = [row for row in summary if row["pass_rate"] == 1.0 and row["last_status"] == "done"]
imperfect = [row for row in summary if row not in perfect]

newest = max((r.get("ts") or 0) for r in records)
age = time.time() - newest if newest else None
when = f", newest record {datetime.fromtimestamp(newest).strftime('%Y-%m-%d %H:%M')}" if newest else ""
print(f"{len(records)} records, {len(summary)} URLs{when}\n")

for row in imperfect:
    print(f"{row['pass_rate']:>6.0%}  {row['passed']}/{row['runs']}  {row['last_status']:<20} {row['url']}")
    if row["last_detail"]:
        print(f"        {row['last_detail']}")
    if row["last_heal_events"]:
        print("        (healing fired in the last run)")

if not imperfect:
    print("Every URL passed every recorded run.")
elif perfect:
    print(f"\n{len(perfect)} further URLs passed every recorded run." +
          ("" if args.all else " Use --all to list them."))
if args.all:
    for row in perfect:
        print(f"{row['pass_rate']:>6.0%}  {row['passed']}/{row['runs']}  {row['last_status']:<20} {row['url']}")
