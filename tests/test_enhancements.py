"""Offline contracts for the local enhancements: typed budgets, stall guard, retries,
error evidence, tab visibility, and post-run expectations. No paid APIs."""

import time
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import reporting
from jev_ultrafast.agent import BudgetExhausted, Stalled
from jev_ultrafast.browser import Browser, StalePage
from tests.test_agent import decision, fingerprint, page


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    p = page()
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p,
        "decision": None,
        "goal": "Find a book",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
        "max_actions": 60,
        "max_decisions": 120,
        "max_stall": 3,
        "choose_retries": 0,
        "stalled_ticks": 0,
    }
    return a


def test_action_budget_stops_with_typed_status(runner):
    runner.state["history"] = [{"step": 1, "action": "Go", "kind": "click"}]
    runner.state["max_actions"] = 1
    runner.state["decision"] = decision("e3")
    with pytest.raises(BudgetExhausted, match="demo budget"):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    assert runner.state["browser"].act.call_count == 0  # The budget stops before execution.


def test_decision_budget_stops_with_typed_status(runner):
    runner.state["max_decisions"] = 0
    with pytest.raises(BudgetExhausted, match="model-call budget"):
        runner.command("predict", {})


def test_stall_guard_stops_after_consecutive_no_op_decisions(runner, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e3")))
    runner.state["browser"].act.side_effect = StalePage("Target is covered")
    runner.state["max_stall"] = 2
    runner.command("tick")
    assert runner.state["status"] == "ready" and runner.state["stalled_ticks"] == 1
    with pytest.raises(Stalled, match="consecutive decisions"):
        runner.command("tick")
    assert runner.state["status"] == "blocked"


def test_executed_action_resets_the_stall_counter(runner, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e3")))
    runner.state["browser"].act.side_effect = [StalePage("Target is covered"), None]
    runner.state["max_stall"] = 5
    runner.command("tick")
    runner.command("tick")
    assert runner.state["stalled_ticks"] == 0
    assert len(runner.state["history"]) == 1


def test_invalid_model_response_is_retried_and_succeeds(runner, monkeypatch):
    choose = Mock(side_effect=[ValueError("Invalid TypeSafe response; no action executed."), decision()])
    monkeypatch.setattr(loop, "choose", choose)
    runner.state["choose_retries"] = 1
    runner.command("predict", {})
    assert choose.call_count == 2
    assert len(runner.state["decisions"]) == 1


def test_unrelated_model_failure_is_not_retried(runner, monkeypatch):
    choose = Mock(side_effect=ValueError("Model connection failed; no action executed."))
    monkeypatch.setattr(loop, "choose", choose)
    runner.state["choose_retries"] = 2
    with pytest.raises(ValueError, match="Model connection failed"):
        runner.command("predict", {})
    assert choose.call_count == 1


def make_browser():
    b = Browser.__new__(Browser)
    b.session = "s1"
    b.collect_errors = True
    b.auth_origin = None
    b.errors = []
    b.error_counts = {}
    b._request_urls = {}
    return b


def test_error_events_become_evidence(monkeypatch):
    b = make_browser()
    monkeypatch.setattr("jev_ultrafast.browser.drain_events", Mock(return_value=[
        {"session_id": "s1", "method": "Runtime.exceptionThrown",
         "params": {"exceptionDetails": {"text": "Uncaught", "exception": {"description": "TypeError: x"}}}},
        {"session_id": "other", "method": "Runtime.exceptionThrown",
         "params": {"exceptionDetails": {"text": "not ours"}}},
        {"session_id": "s1", "method": "Runtime.consoleAPICalled",
         "params": {"type": "error", "args": [{"type": "string", "value": "boom"}]}},
        {"session_id": "s1", "method": "Runtime.consoleAPICalled",
         "params": {"type": "warning", "args": []}},  # warnings are not errors
        {"session_id": "s1", "method": "Log.entryAdded",
         "params": {"entry": {"level": "error", "text": "Failed to load", "url": "https://x/y.js"}}},
        {"session_id": "s1", "method": "Network.requestWillBeSent",
         "params": {"requestId": "r1", "request": {"url": "https://x/api"}}},
        {"session_id": "s1", "method": "Network.loadingFailed",
         "params": {"requestId": "r1", "errorText": "net::ERR_FAILED", "type": "Fetch"}},
        {"session_id": "s1", "method": "Network.responseReceived",
         "params": {"response": {"status": 503, "url": "https://x/slow"}}},
    ]))
    b._drain()
    assert b.error_counts == {"exception": 1, "console": 2, "request_failed": 1, "http_status": 1}
    summary = b.error_summary()
    assert summary["counts"]["exception"] == 1
    failed = next(e for e in summary["items"] if e["kind"] == "request_failed")
    assert failed["url"] == "https://x/api"  # The failed request is identified by its URL.


def test_error_capture_can_be_disabled(monkeypatch):
    b = make_browser()
    b.collect_errors = False
    drain = Mock(return_value=[{"session_id": "s1", "method": "Runtime.exceptionThrown",
                                "params": {"exceptionDetails": {"text": "x"}}}])
    monkeypatch.setattr("jev_ultrafast.browser.drain_events", drain)
    b._drain()
    assert b.errors == [] and drain.call_count == 0


def test_error_items_are_capped_but_counts_keep_rising():
    b = make_browser()
    for i in range(150):
        b._record("console", f"e{i}")
    assert len(b.errors) == 100
    assert b.error_counts["console"] == 150


def test_keep_open_preserves_the_tab(monkeypatch):
    b = Browser.__new__(Browser)
    b.target = "t1"
    b.keep_open = True
    cdp = Mock()
    monkeypatch.setattr("jev_ultrafast.browser.cdp", cdp)
    b.close()
    cdp.assert_not_called()
    b.keep_open = False
    b.target = "t1"
    b.close()
    assert cdp.call_args.args == ("Target.closeTarget",)
    assert b.target is None


def test_expectations_check_the_last_observed_page():
    page_state = {"url": "https://x/search?q=book", "text": "12 results for book"}
    assert reporting.check_expectations(page_state, expect_url="q=book") == {"url": True}
    assert reporting.check_expectations(page_state, expect_text="0 results") == {"text": False}
    both = reporting.check_expectations(page_state, expect_url="q=book", expect_text="12 results")
    assert both == {"url": True, "text": True}


def test_records_are_json_serializable_and_complete():
    record = reporting.build_record(
        url="https://x/", status="done", page={"url": "https://x/about"},
        actions=5, decisions=7, elapsed_ms=900,
        errors={"counts": {"console": 1}, "items": [{"kind": "console", "text": "e", "url": ""}]},
        expectations={"url": True},
    )
    assert record["final_url"] == "https://x/about"
    assert record["expectations"] == {"url": True}
    assert record["errors"]["counts"] == {"console": 1}
    import json

    assert json.loads(json.dumps(record)) == record


def test_stale_navigation_tick_still_counts_toward_stall(runner, monkeypatch):
    # A navigation interrupts predict; the re-observe path is the same no-progress path as act failures.
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e3")))
    runner.state["browser"].fresh.side_effect = StalePage("Document navigating")
    runner.state["max_stall"] = 1
    with pytest.raises(Stalled):
        runner.command("tick")
    assert runner.state["status"] == "blocked"


def test_fingerprint_helper_is_untouched():
    p = page()
    assert fingerprint(p) == fingerprint(p)


def test_dwell_is_off_by_default_and_opt_in_via_environment(runner, monkeypatch):
    sleeps = []
    monkeypatch.setattr("jev_ultrafast.agent.time.sleep", sleeps.append)
    monkeypatch.delenv("JEV_DWELL_MS", raising=False)
    loop._dwell(runner.state)
    assert sleeps == []  # the loop stays ultrafast unless the operator asks for pacing
    monkeypatch.setenv("JEV_DWELL_MS", "200-400")
    loop._dwell(runner.state)
    assert 0.2 <= sleeps[-1] <= 0.4
    runner.state["history"] = [{"page_changed": True}]
    loop._dwell(runner.state)
    assert 0.3 <= sleeps[-1] <= 0.6  # 1.5x after the page changed
    monkeypatch.setenv("JEV_DWELL_MS", "nonsense")
    with pytest.raises(ValueError, match="JEV_DWELL_MS"):
        loop._dwell(runner.state)
