"""Post-run verification and structured records. A model judgment gets checked against evidence."""

from pathlib import Path

SPEC_KEYS = ("expect-url", "expect-text")


def parse_page_spec(line):
    """One sweep line: 'URL [expect-url=SUBSTRING] [expect-text=SUBSTRING]'.

    Returns (url, expect_url, expect_text) so a --urls file doubles as the
    per-page expectation suite. Expectations are substrings the final observed
    page must contain; the runner fails the row when they do not.
    """
    tokens = line.split()
    if not tokens:
        raise ValueError("Empty page spec")
    url, expect_url, expect_text = tokens[0], None, None
    for token in tokens[1:]:
        key, sep, value = token.partition("=")
        if not sep or key not in SPEC_KEYS or not value:
            raise ValueError(
                f"Bad page-spec token {token!r}; expected expect-url=SUBSTRING or expect-text=SUBSTRING"
            )
        if key == "expect-url":
            expect_url = value
        else:
            expect_text = value
    return url, expect_url, expect_text


def parse_pages_file(path):
    """Parse a --urls file: one spec per line, '#' comments and blank lines skipped."""
    specs = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        line = stripped.split(" #", 1)[0].strip()
        if line:
            specs.append(parse_page_spec(line))
    return specs


def record_passed(record):
    """A row is verified when its expectations all hold; without expectations, done counts."""
    expectations = record.get("expectations")
    if expectations is not None:
        return all(expectations.values())
    return record["status"] == "done"


def summarize_records(records):
    """Per-URL stability across appended sweep runs, for long-term trend evidence.

    Returns one row per URL, flakiest first: run count, pass rate, and the most
    recent outcome with its evidence.
    """
    by_url = {}
    for record in records:
        entry = by_url.setdefault(record["url"], {"runs": 0, "passed": 0, "last": None})
        entry["runs"] += 1
        if record_passed(record):
            entry["passed"] += 1
        entry["last"] = record
    rows = []
    for url, entry in by_url.items():
        last = entry["last"]
        rows.append({
            "url": url,
            "runs": entry["runs"],
            "passed": entry["passed"],
            "pass_rate": round(entry["passed"] / entry["runs"], 3),
            "last_status": last["status"],
            "last_detail": (last.get("detail") or "")[:140],
            "last_heal_events": len(last.get("heal_events") or []),
            "last_ts": last.get("ts"),
        })
    return sorted(rows, key=lambda row: (row["pass_rate"], row["url"]))


def check_expectations(page, expect_url=None, expect_text=None):
    """Substring checks over the last observed page. Only requested checks appear in the result."""
    page = page or {}
    checks = {}
    if expect_url:
        checks["url"] = expect_url in page.get("url", "")
    if expect_text:
        checks["text"] = expect_text in page.get("text", "")
    return checks


def build_record(*, url, status, detail="", page=None, actions=0, decisions=0, elapsed_ms=0,
                 errors=None, expectations=None, heal_events=None):
    """One JSON-serializable outcome row: the judgment, its evidence, and any expectation results."""
    return {
        "url": url,
        "status": status,
        "detail": detail,
        "final_url": (page or {}).get("url"),
        "actions": actions,
        "decisions": decisions,
        "elapsed_ms": elapsed_ms,
        "errors": errors or {"counts": {}, "items": []},
        "expectations": expectations,
        "heal_events": heal_events or [],
    }
