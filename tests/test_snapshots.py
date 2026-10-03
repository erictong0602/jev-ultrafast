"""Offline contracts for cookie snapshots and e2e step drafts. No browser, no paid APIs."""

import json
from unittest.mock import Mock

import jev_ultrafast.browser as browser_mod
from jev_ultrafast.browser import Browser
from jev_ultrafast.reporting import steps_from_history


def browser_with():
    b = Browser.__new__(Browser)
    b.session = "test"
    b.session_events = []
    b._network_enabled = True
    return b


def test_save_writes_origin_scoped_snapshot(tmp_path, monkeypatch):
    cookies = [{"name": "sid", "value": "s3cret", "domain": ".example.test", "path": "/"}]

    def cdp(method, **params):
        if method == "Network.getCookies":
            return {"cookies": cookies}
        return {}

    monkeypatch.setattr(browser_mod, "cdp", cdp)
    path = tmp_path / "snapshots" / "staging.json"
    b = browser_with()
    assert b.save_cookies("https://example.test/", path) == 1
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["origin"] == "https://example.test/"
    assert data["cookies"][0]["name"] == "sid"
    assert data["saved_at"] > 0


def test_restore_replays_snapshot_cookies(tmp_path, monkeypatch):
    calls = []

    def cdp(method, **params):
        calls.append((method, params))
        return {}

    monkeypatch.setattr(browser_mod, "cdp", cdp)
    path = tmp_path / "snap.json"
    path.write_text(json.dumps({"origin": "https://example.test/", "saved_at": 1, "cookies": [
        {"name": "sid", "value": "s3cret", "domain": ".example.test", "path": "/"},
    ]}), encoding="utf-8")
    b = browser_with()
    assert b.restore_cookies(path) == 1
    assert calls[0][0] == "Storage.setCookies"
    assert calls[0][1]["cookies"][0]["name"] == "sid"


def test_restore_of_an_empty_snapshot_is_a_noop(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"origin": "https://x/", "cookies": []}), encoding="utf-8")
    b = browser_with()
    b._maintenance_call = Mock(side_effect=AssertionError("must not call CDP"))
    assert b.restore_cookies(path) == 0


def test_steps_draft_records_operations_targets_and_text():
    history = [
        {"step": 1, "kind": "fill", "action": "Where from?", "text": "Zurich", "url": "https://x/f",
         "page_changed": False},
        {"step": 2, "kind": "click", "action": "Search", "text": None, "url": "https://x/results",
         "page_changed": True},
        {"step": 3, "kind": "wait", "action": "Wait", "text": None, "url": "https://x/results",
         "page_changed": False},
    ]
    steps = steps_from_history(history)
    assert steps[0] == {"n": 1, "operation": "TYPE_TEXT", "target": "Where from?",
                        "url_after": "https://x/f", "page_changed": False, "text": "Zurich"}
    assert steps[1] == {"n": 2, "operation": "CLICK", "target": "Search",
                        "url_after": "https://x/results", "page_changed": True}
    assert "text" not in steps[2] and steps[2]["operation"] == "WAIT"


def test_close_survives_a_daemon_hiccup(monkeypatch):
    def cdp(method, **params):
        raise RuntimeError("daemon went away")

    monkeypatch.setattr(browser_mod, "cdp", cdp)
    b = Browser.__new__(Browser)
    b.keep_open = False
    b.target = "t1"
    b.close()  # Must not raise.
    assert b.target is None
