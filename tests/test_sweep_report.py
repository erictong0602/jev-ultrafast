"""Offline contracts for long-run evidence: pass logic and per-URL trend summaries."""

from jev_ultrafast.reporting import build_record, record_passed, summarize_records


def record(url, status="done", expectations=None, detail="", heal_events=None, ts=1000):
    return build_record(
        url=url, status=status, detail=detail, expectations=expectations, heal_events=heal_events,
    ) | {"ts": ts}


def test_pass_without_expectations_means_done():
    assert record_passed(record("https://x/", "done")) is True
    assert record_passed(record("https://x/", "blocked")) is False
    assert record_passed(record("https://x/", "timeout")) is False


def test_expectations_overrule_status():
    assert record_passed(record("https://x/", "done", expectations={"text": False})) is False
    assert record_passed(record("https://x/", "blocked", expectations={"text": True})) is True


def test_heal_events_and_ts_travel_in_the_record():
    row = record("https://x/", heal_events=["reload restored the session"], ts=1700000000)
    assert row["heal_events"] == ["reload restored the session"]
    assert row["ts"] == 1700000000


def test_summary_reports_flakiest_pages_first():
    rows = [
        record("https://x/stable", "done", expectations={"text": True}, ts=1),
        record("https://x/flaky", "done", expectations={"text": True}, ts=1),
        record("https://x/flaky", "failed_expectation", expectations={"text": False}, ts=2),
        record("https://x/dead", "timeout", ts=3),
    ]
    summary = summarize_records(rows)
    assert [row["url"] for row in summary] == ["https://x/dead", "https://x/flaky", "https://x/stable"]
    flaky = summary[1]
    assert flaky["runs"] == 2 and flaky["passed"] == 1 and flaky["pass_rate"] == 0.5
    assert flaky["last_status"] == "failed_expectation" and flaky["last_ts"] == 2


def test_summary_counts_heal_activity():
    rows = [record("https://x/", "done", heal_events=["reload restored the session"])]
    assert summarize_records(rows)[0]["last_heal_events"] == 1
