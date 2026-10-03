"""Offline contracts for session persistence and healing. No browser, no paid APIs."""

from unittest.mock import Mock

import pytest

import jev_ultrafast.agent as loop
import jev_ultrafast.browser as browser_mod
from jev_ultrafast.browser import Browser


def browser_with(document_status=200, document_url="https://example.test/dashboard"):
    b = Browser.__new__(Browser)
    b.session = "test"
    b.document_status = document_status
    b.document_url = document_url
    b.session_events = []
    b._network_enabled = True
    return b


@pytest.fixture(autouse=True)
def no_login_pattern(monkeypatch):
    monkeypatch.delenv("JEV_LOGIN_URL_PATTERN", raising=False)


def test_wedged_page_does_not_leak_its_tab(monkeypatch):
    methods = []
    state = {"fail_navigate": True}

    def cdp(method, **params):
        methods.append(method)
        if method == "Target.createTarget":
            return {"targetId": "t1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method == "Page.navigate" and state["fail_navigate"]:
            raise RuntimeError("Page.navigate timed out after 5s")
        if method == "Runtime.evaluate":
            return {"result": {"value": "complete"}}
        return {}

    monkeypatch.setattr(browser_mod, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser_mod, "cdp", cdp)
    monkeypatch.delenv("JEV_BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("JEV_BASIC_AUTH_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="timed out"):
        Browser("https://example.test/", collect_errors=False, heal_session=False)
    assert "Target.closeTarget" in methods  # The tab died with the failed init.

    methods.clear()
    state["fail_navigate"] = False
    b = Browser("https://example.test/", collect_errors=False, heal_session=False)
    assert "Target.closeTarget" not in methods  # A healthy init opens nothing extra.
    b.close()
    assert methods.count("Target.closeTarget") == 1


@pytest.mark.parametrize("status,alive", [(200, True), (302, True), (500, True), (401, False), (403, False)])
def test_document_status_alone_decides_the_verdict(status, alive):
    assert browser_with(status).session_ok("https://example.test/") is alive


def test_login_pattern_detects_a_soft_logout(monkeypatch):
    monkeypatch.setenv("JEV_LOGIN_URL_PATTERN", r"/(login|signin)")
    b = browser_with(200, "https://example.test/login?next=/dashboard")
    assert b.session_ok("https://example.test/") is False
    assert browser_with(200).session_ok("https://example.test/other") is True


def test_heal_keeps_cookies_when_a_reload_restores_the_session():
    b = browser_with(401)
    b.session_ok = Mock(side_effect=[False, True])
    b._load = Mock()
    b.clear_cookies = Mock()
    b.clear_cache = Mock()
    b.heal("https://example.test/")
    assert b._load.call_count == 1  # One reload with the session intact.
    b.clear_cookies.assert_not_called()
    b.clear_cache.assert_not_called()
    assert b.session_events == ["landing looks logged out", "reload restored the session"]


def test_heal_clears_only_this_origin_after_the_reload_fails():
    b = browser_with(401)
    b.session_ok = Mock(side_effect=[False, False])
    b._load = Mock()
    b.cookies = Mock(return_value=[{"name": "sid", "domain": ".example.test", "path": "/"}])
    b.clear_cookies = Mock()
    b.clear_cache = Mock()
    b.heal("https://example.test/dashboard")
    b.clear_cookies.assert_called_once_with("https://example.test/dashboard")
    b.clear_cache.assert_called_once()
    assert b._load.call_count == 2  # Reload, then a clean load after clearing.
    assert b.session_events == [
        "landing looks logged out",
        "cleared 1 cookie(s) and cache for example.test",
        "origin storage cleared; the next sign-in persists",
    ]


def test_heal_reports_a_login_wall_by_pattern_after_clearing(monkeypatch):
    monkeypatch.setenv("JEV_LOGIN_URL_PATTERN", r"/login")
    b = browser_with(200, "https://example.test/login")
    b.session_ok = Mock(side_effect=[False, False, False])
    b._load = Mock()
    b.cookies = Mock(return_value=[])
    b.clear_cookies = Mock()
    b.clear_cache = Mock()
    b.heal("https://example.test/")
    assert b.session_events[-1] == "still behind the login wall; sign in once in the tab"


def test_origin_scoped_clear_never_touches_other_sites(monkeypatch):
    calls = []

    def cdp(method, **params):
        calls.append((method, params))
        if method == "Network.getCookies":
            return {"cookies": [{"name": "sid", "domain": ".example.test", "path": "/"}]}
        return {}

    monkeypatch.setattr(browser_mod, "cdp", cdp)
    b = browser_with()
    b.clear_cookies("https://example.test/page")
    assert [method for method, _ in calls] == ["Network.getCookies", "Network.deleteCookies"]
    # Maintenance round trips get a generous response timeout; cache eviction can be slow.
    assert calls[0][1]["_response_timeout"] == 30
    def wire(params):
        return {k: v for k, v in params.items() if k not in {"session_id", "_response_timeout"}}
    assert wire(calls[0][1]) == {"urls": ["https://example.test", "https://example.test/page"]}
    assert wire(calls[1][1]) == {"name": "sid", "domain": ".example.test", "path": "/"}


def test_lazy_network_enable_serves_cookie_tools_on_a_quiet_session(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_mod, "cdp", lambda method, **params: calls.append(method) or {})
    b = browser_with()
    b._network_enabled = False
    b.clear_cookies(None)
    b.clear_cache()
    assert calls == ["Network.enable", "Network.clearBrowserCookies", "Network.clearBrowserCache"]


def test_unscoped_clear_uses_browser_wide_reads(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_mod, "cdp", lambda method, **params: calls.append(method) or {})
    b = browser_with()
    assert len(b.cookies(None)) == 0
    b.clear_cookies(None)
    b.clear_cache()
    assert calls == ["Storage.getCookies", "Network.clearBrowserCookies", "Network.clearBrowserCache"]


def test_document_status_is_taken_from_the_main_document_only(monkeypatch):
    b = Browser.__new__(Browser)
    b.collect_errors = True
    b.auth_origin = None
    b.document_status = None
    b.document_url = ""
    b.error_counts = {}
    b.errors = []
    b._request_urls = {}
    b.session = "test"
    b._network_enabled = True
    monkeypatch.setattr(browser_mod, "drain_events", lambda: [
        {"session_id": "test", "method": "Network.responseReceived", "params": {
            "type": "Script", "response": {"status": 404, "url": "https://example.test/missing.js"}}},
        {"session_id": "test", "method": "Network.responseReceived", "params": {
            "type": "Document", "response": {"status": 200, "url": "https://example.test/"}}},
    ])
    b._drain()
    assert b.document_status == 200
    assert b.document_url == "https://example.test/"
    assert b.session_ok("https://example.test/") is True
    assert b.error_counts == {"http_status": 1}  # The 404 stays in the error evidence.


def test_agent_exposes_heal_session():
    assert "heal_session" in loop.Agent.__init__.__code__.co_varnames
