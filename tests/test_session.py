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
    monkeypatch.delenv("BU_CDP_WS", raising=False)
    monkeypatch.delenv("BU_CDP_URL", raising=False)


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


class FakePage:
    """Enough of a document for Browser._load. A reload keeps its URL, so document
    identity has to come from performance.timeOrigin and not from location.href."""

    def __init__(self, href="https://protected.example.com/dashboard"):
        self.href = href
        self.time_origin = 1.0

    def evaluate(self, expression):
        if "performance.timeOrigin" in expression:
            return self.time_origin
        if "location.assign" in expression:
            self.time_origin += 1  # A navigation: the next document is a new one.
            return "navigating"
        if "location.href" in expression:
            return self.href
        return "complete"

    def reload(self, href=None):
        """A reload: same URL, new document."""
        self.href = href or self.href
        self.time_origin += 1


def browser_with_auth(monkeypatch, url, page=None, **env):
    """Construct a Browser with basic-auth env applied; return every protocol call made.

    The fake daemon behaves like Chrome 154: once the navigation is kicked off,
    the drain yields the auth challenge and then the renderer-swap event burst
    (detach after the pause, re-attach right after)."""
    page = page or FakePage()
    methods = []
    scope = {s.strip() for s in env.get("JEV_BASIC_AUTH_ORIGIN", "").split(",") if s.strip()}
    netloc = url.split("://", 1)[1].split("/", 1)[0] if "://" in url else url
    auth_applies = not scope or netloc in scope
    state = {"nav": False, "served": False}

    def cdp(method, **params):
        methods.append((method, params))
        if method == "Target.createTarget":
            return {"targetId": "t1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method == "Runtime.evaluate":
            if "location.assign" in params.get("expression", ""):
                state["nav"] = True
            return {"result": {"value": page.evaluate(params.get("expression", ""))}}
        return {}

    def fake_drain():
        if not state["served"] and state["nav"] and auth_applies:
            state["served"] = True
            return [
                {"session_id": "s1", "method": "Fetch.authRequired",
                 "params": {"request": {"url": url}, "requestId": "r1"}},
                {"session_id": "s1", "method": "Inspector.detached",
                 "params": {"reason": "Render process gone"}},
                {"method": "Target.detachedFromTarget", "params": {"sessionId": "s1"}},
                {"method": "Target.attachedToTarget",
                 "params": {"sessionId": "s2", "targetInfo": {"targetId": "t1", "type": "page"}}},
            ]
        return []

    monkeypatch.setattr(browser_mod, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser_mod, "cdp", cdp)
    monkeypatch.setattr(browser_mod, "drain_events", fake_drain)
    monkeypatch.setenv("JEV_BASIC_AUTH_USERNAME", "user")
    monkeypatch.setenv("JEV_BASIC_AUTH_PASSWORD", "pass")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    Browser(url, collect_errors=False, heal_session=False).close()
    return methods


def fetch_enables(methods):
    """The interception policy only; session routing is not part of the contract."""
    return [
        {key: value for key, value in params.items() if key != "session_id"}
        for method, params in methods if method == "Fetch.enable"
    ]


def evaluate_expressions(methods):
    return [params.get("expression", "") for method, params in methods if method == "Runtime.evaluate"]


def test_basic_auth_intercepts_documents_only_at_request_stage(monkeypatch):
    methods = browser_with_auth(monkeypatch, "https://protected.example.com/dashboard")
    assert fetch_enables(methods) == [
        {
            "handleAuthRequests": True,
            "patterns": [
                {
                    "urlPattern": "https://protected.example.com/*",
                    "requestStage": "Request",
                    "resourceType": "Document",
                }
            ],
        }
    ]


def test_basic_auth_origin_scopes_interception_to_named_hosts(monkeypatch):
    assert fetch_enables(browser_with_auth(
        monkeypatch, "https://secrets.example.dev/", JEV_BASIC_AUTH_ORIGIN="protected.example.com"
    )) == []  # Landing origin is not the protected one: no interception at all.
    enables = fetch_enables(browser_with_auth(
        monkeypatch,
        "https://protected.example.com/",
        JEV_BASIC_AUTH_ORIGIN="protected.example.com,other.example.com",
    ))
    assert enables and enables[0]["patterns"][0]["urlPattern"] == "https://protected.example.com/*"


def test_basic_auth_navigation_never_blocks_on_page_navigate(monkeypatch):
    """The challenge that answers an intercepted navigation arrives as an event, so a
    command that waits for the navigation to resolve can never be answered: the document
    stays put and every later call queues behind it until the daemon gives up."""
    methods = browser_with_auth(monkeypatch, "https://protected.example.com/dashboard")
    assert "Page.navigate" not in [method for method, _ in methods]
    assert any("location.assign" in expression for expression in evaluate_expressions(methods))


def test_plain_navigation_still_uses_page_navigate(monkeypatch):
    methods = browser_with_auth(monkeypatch, "https://example.test/", JEV_BASIC_AUTH_ORIGIN="other.example.com")
    assert "Page.navigate" in [method for method, _ in methods]


def test_a_reload_of_the_same_url_still_counts_as_a_new_document(monkeypatch):
    page = FakePage()
    b = Browser.__new__(Browser)
    monkeypatch.setattr(Browser, "evaluate", lambda self, expression: page.evaluate(expression))
    before = page.evaluate("performance.timeOrigin")
    assert b._document_replaced(before) is False  # Nothing moved yet.
    page.reload()  # Same URL, new document: a URL comparison would have waited out the deadline.
    assert b._document_replaced(before) is True
    page.href = "about:blank"
    assert b._document_replaced(None) is False  # No marker: about:blank proves nothing.
    page.href = "https://protected.example.com/dashboard"
    assert b._document_replaced(None) is True


def drain_browser():
    b = Browser.__new__(Browser)
    b.session = "ours"
    b.auth_origin = "protected.example.com"
    b.auth_username = "user"
    b.auth_password = "pass"
    b.auth_challenges = 0
    b.collect_errors = True
    b.errors = []
    b.error_counts = {}
    b._request_urls = {}
    b.document_status = None
    b.document_url = ""
    return b


def test_drain_answers_the_challenge_and_flags_fetch(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_mod, "drain_events", lambda: [
        {"session_id": "ours", "method": "Fetch.authRequired",
         "params": {"request": {"url": "https://protected.example.com/a"}, "requestId": "r1"}},
    ])
    monkeypatch.setattr(browser_mod, "cdp", lambda method, **params: calls.append((method, params)) or {})
    b = drain_browser()
    assert b._drain() == {"fetch": True}
    assert calls[0][0] == "Fetch.continueWithAuth"
    assert calls[0][1]["authChallengeResponse"]["username"] == "user"


def test_drain_other_origins_challenges_are_cancelled(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_mod, "drain_events", lambda: [
        {"session_id": "ours", "method": "Fetch.authRequired",
         "params": {"request": {"url": "https://elsewhere.example/x"}, "requestId": "r2"}},
    ])
    monkeypatch.setattr(browser_mod, "cdp", lambda method, **params: calls.append((method, params)) or {})
    b = drain_browser()
    assert b._drain() == {"fetch": True}
    assert calls[0][1]["authChallengeResponse"]["response"] == "CancelAuth"


def test_drain_flags_the_renderer_swap_from_browser_level_events(monkeypatch):
    """The swap events are browser-level (no session id on the envelope), so the
    drain matches them by their params instead of dropping them with the rest."""
    monkeypatch.setattr(browser_mod, "drain_events", lambda: [
        {"method": "Target.detachedFromTarget", "params": {"sessionId": "ours"}},
        {"method": "Target.attachedToTarget",
         "params": {"sessionId": "new", "targetInfo": {"targetId": "t1"}}},
        {"method": "Target.attachedToTarget",
         "params": {"sessionId": "x", "targetInfo": {"targetId": "someone-else"}}},
    ])
    b = drain_browser()
    b.target = "t1"
    assert b._drain() == {"detached": True, "reattached": True}


def test_no_credentials_means_no_interception(monkeypatch):
    monkeypatch.delenv("JEV_BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("JEV_BASIC_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("JEV_BASIC_AUTH_ORIGIN", raising=False)
    methods = []

    def cdp(method, **params):
        methods.append(method)
        if method == "Target.createTarget":
            return {"targetId": "t1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method == "Runtime.evaluate":
            return {"result": {"value": "complete"}}
        return {}

    monkeypatch.setattr(browser_mod, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser_mod, "cdp", cdp)
    Browser("https://example.test/", collect_errors=False, heal_session=False).close()
    assert "Fetch.enable" not in methods


def patch_daemon(monkeypatch, attached_ids, wanted_ids):
    monkeypatch.setattr(browser_mod, "daemon_alive", lambda: True)
    monkeypatch.setattr(browser_mod, "_endpoint_page_ids", lambda endpoint: wanted_ids)
    monkeypatch.setattr(
        browser_mod, "cdp",
        lambda method, **params: {"targetInfos": [{"targetId": name, "type": "page"} for name in attached_ids]},
    )
    calls = []
    monkeypatch.setattr(browser_mod, "ensure_daemon", lambda: calls.append("ensure"))
    return calls


def test_session_with_its_own_endpoint_refuses_a_foreign_daemon(monkeypatch):
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9334")
    calls = patch_daemon(monkeypatch, attached_ids={"tab-on-9333"}, wanted_ids={"tab-on-9334"})
    with pytest.raises(RuntimeError, match="different browser"):
        browser_mod.ensure_isolated_daemon()
    assert calls == []  # The live daemon serving another Chrome is never reused.


def test_daemon_serving_the_requested_endpoint_is_reused(monkeypatch):
    monkeypatch.setenv("BU_CDP_WS", "ws://127.0.0.1:9333/devtools/browser/abc")
    calls = patch_daemon(monkeypatch, attached_ids={"tab-a", "tab-b"}, wanted_ids={"tab-b", "tab-c"})
    browser_mod.ensure_isolated_daemon()
    assert calls == ["ensure"]  # Same Chrome instance: intersecting target IDs prove it.


def test_no_endpoint_env_defers_to_plain_ensure_daemon(monkeypatch):
    calls = patch_daemon(monkeypatch, attached_ids=set(), wanted_ids=set())
    browser_mod.ensure_isolated_daemon()
    assert calls == ["ensure"]  # Local discovery is unchanged.


def test_unreachable_named_endpoint_fails_loudly(monkeypatch):
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9334")

    def unreachable(endpoint):
        raise OSError("connection refused")

    monkeypatch.setattr(browser_mod, "daemon_alive", lambda: True)
    monkeypatch.setattr(browser_mod, "_endpoint_page_ids", unreachable)
    with pytest.raises(RuntimeError, match="not reachable"):
        browser_mod.ensure_isolated_daemon()


def test_stale_daemon_defers_to_self_heal(monkeypatch):
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9334")
    calls = patch_daemon(monkeypatch, attached_ids={"tab-on-9333"}, wanted_ids={"tab-on-9334"})

    def stale(method, **params):
        raise RuntimeError("CDP WS is dead")

    monkeypatch.setattr(browser_mod, "cdp", stale)
    browser_mod.ensure_isolated_daemon()
    assert calls == ["ensure"]  # ensure_daemon's staleness handling replaces it.


def test_endpoint_base_maps_ws_schemes_to_http(monkeypatch):
    monkeypatch.setenv("BU_CDP_WS", "ws://127.0.0.1:9333/devtools/browser/abc")
    assert browser_mod._desired_endpoint_base() == "http://127.0.0.1:9333"
    monkeypatch.setenv("BU_CDP_WS", "wss://proxy.example.test/devtools/browser/abc")
    assert browser_mod._desired_endpoint_base() == "https://proxy.example.test"
    monkeypatch.delenv("BU_CDP_WS")
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9334/")
    assert browser_mod._desired_endpoint_base() == "http://127.0.0.1:9334"
    monkeypatch.delenv("BU_CDP_URL")
    assert browser_mod._desired_endpoint_base() is None
