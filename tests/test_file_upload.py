"""Offline contracts for agent-driven file attachment. No browser, no paid APIs."""

import pytest

import jev_ultrafast.browser as browser_mod
from jev_ultrafast.browser import StalePage, browser_operation
from jev_ultrafast.model import action_space, choose


def file_action():
    return {"id": "e9", "kind": "file", "label": "Upload resume", "role": "file", "value": "", "node": 30}


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0,
            "probabilities": {i: float(i == selected) for i in ids}}


def test_file_inputs_get_their_own_operation_head():
    elements, targets, _ = action_space([file_action()])
    assert len(elements) == 1 and elements[0]["operations"] == ["SET_FILE"]
    assert targets["SET_FILE"]["1"]["id"] == "e9"


def test_set_file_target_head_is_offered_and_executed(monkeypatch):
    def post(_url, _key, body):
        assert "set_file_target" in body["questions"], "the head ships in the same request"
        criteria = body["questions"]["operation"]["criteria"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(criteria, "SET_FILE"),
                "set_file_target": choice(body["questions"]["set_file_target"]["criteria"], "1"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr("jev_ultrafast.model.post_json", post)
    state = {"url": "https://x/", "title": "t", "text": "Upload", "scroll": {"y": 0},
             "actions": [file_action()]}
    decision = choose(state, "Upload the resume", [])
    assert decision["choice"] == "e9" and decision["operation"] == "SET_FILE"


def test_execution_bypasses_the_chooser_and_sets_files(monkeypatch):
    calls = []

    def cdp(method, session_id=None, **params):
        calls.append((method, params))
        if method == "Runtime.evaluate" and params.get("returnByValue") is False:
            return {"result": {"objectId": "obj-1"}}
        if method == "DOM.requestNode":
            return {"nodeId": 77}
        if method == "Runtime.evaluate":
            value = {"x": 40, "y": 20} if "action" in params.get("expression", "") else 1
            return {"result": {"value": value}}
        return {}

    monkeypatch.setattr(browser_mod, "cdp", cdp)
    result = browser_operation({"operation": "act", "session": "s", "files": ["C:/docs/resume.pdf"],
                                "action": file_action()})
    assert result == {"executed": "e9"}
    methods = [m for m, _ in calls]
    assert "Input.dispatchMouseEvent" not in methods, "no native chooser may open"
    set_files = next(p for m, p in calls if m == "DOM.setFileInputFiles")
    assert set_files == {"files": ["C:/docs/resume.pdf"], "nodeId": 77}
    assert any("dispatchEvent" in p.get("expression", "") for _, p in calls), "change must reach the page"


def test_missing_operator_files_stops_before_any_mutation(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_mod, "cdp", lambda method, session_id=None, **params:
                        calls.append(method) or {})
    with pytest.raises(ValueError, match="operator-provided"):
        browser_operation({"operation": "act", "session": "s", "files": [], "action": file_action()})
    assert calls == [], "no browser call may happen without files"


def test_unconfirmed_attachment_is_a_typed_stale(monkeypatch):
    def cdp(method, session_id=None, **params):
        if method == "Runtime.evaluate" and params.get("returnByValue") is False:
            return {"result": {"objectId": "obj-1"}}
        if method == "DOM.requestNode":
            return {"nodeId": 77}
        if method == "Runtime.evaluate":
            value = {"x": 40, "y": 20} if "action" in params.get("expression", "") else 0
            return {"result": {"value": value}}
        return {}

    monkeypatch.setattr(browser_mod, "cdp", cdp)
    with pytest.raises(StalePage, match="not confirmed"):
        browser_operation({"operation": "act", "session": "s", "files": ["a.txt"], "action": file_action()})


def test_agent_rejects_missing_upload_files(tmp_path):
    from jev_ultrafast.agent import Agent

    with pytest.raises(ValueError, match="does not exist"):
        Agent("https://x/", "Upload it", files=[str(tmp_path / "nope.pdf")])


def test_agent_passes_files_only_for_file_actions():
    from jev_ultrafast.agent import Agent

    a = Agent.__new__(Agent)
    seen = {}
    page = {"fingerprint": "f1", "url": "https://x/form", "actions": [
        {"id": "e1", "kind": "click", "label": "Go", "node": 1},
        {"id": "e2", "kind": "file", "label": "Upload resume", "node": 2},
    ]}
    decision = {"choice": "e1", "probabilities": {"e1": 1.0}, "confidence": 1.0,
                "latency_ms": 5, "operation": "CLICK", "target": "1", "usage": {}}

    class FakeBrowser:
        def fresh(self, *_a, **_k):
            return True

        def act(self, action, page, text=None, files=None):
            seen["kind"], seen["files"] = action["kind"], files

        def observe(self, screenshot=False):
            return dict(page, fingerprint="f2")

    a.state = {"browser": FakeBrowser(), "files": ["C:/docs/resume.pdf"], "page": page,
               "decision": decision, "goal": "", "history": [], "status": "predicted", "plan": [""],
               "plan_index": 0, "decisions": [], "text_calls": [], "elapsed_ms": 0,
               "started_at": 0.0, "record": False, "max_actions": 60, "max_decisions": 120,
               "max_stall": 12, "choose_retries": 2, "stalled_ticks": 0}
    a.screenshots = False
    a.command("act", {"fingerprint": "f1"})
    assert seen == {"kind": "click", "files": None}
    a.state["history"].clear()
    a.state["page"]["fingerprint"] = "f1"  # observe() re-issued the page under a new fingerprint.
    a.state["decision"] = {**decision, "choice": "e2", "operation": "SET_FILE",
                           "probabilities": {"e2": 1.0}, "target": "1"}
    a.command("act", {"fingerprint": "f1"})
    assert seen == {"kind": "file", "files": ["C:/docs/resume.pdf"]}
