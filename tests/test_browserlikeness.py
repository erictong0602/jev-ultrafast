"""Offline contracts for browser-likeness handling: no fixed page global, passive error
capture, trusted human-paced input, wheel and cursor plans, and viewport variation.
No paid APIs, no browser."""

import random

import pytest

from jev_ultrafast import browser as browser_mod


def test_snapshot_cache_uses_a_per_process_hidden_key():
    # The placeholder is fully substituted before any expression reaches the page.
    assert "__JEV_KEY__" not in browser_mod.READ_STATE
    assert "__JEV_KEY__" not in browser_mod.MARKER
    assert '"%s"' % browser_mod.FAST_KEY in browser_mod.READ_STATE
    # A constant prefix would be scannable across runs (the cdc_ failure mode), so the
    # key is an anonymous identifier: no double underscore, no tool name, varies per run.
    assert not browser_mod.FAST_KEY.startswith("_")
    assert "jev" not in browser_mod.FAST_KEY.lower()
    assert len(browser_mod.FAST_KEY) == 17


def test_snapshot_js_ships_no_fixed_global():
    text = browser_mod.READ_STATE
    assert "__jevFast" not in text
    # The property is installed non-enumerable, so Object.keys(window) cannot list it.
    assert "enumerable:false" in text
    # The collector copy is returned without clearing; only observe() clears after ingest.
    assert "page_errors:[...cache.page_errors]" in text


def test_error_collector_is_passive_and_keyed():
    assert browser_mod.PAGE_ERROR_COLLECTOR.count(browser_mod.FAST_KEY) >= 1
    assert "addEventListener('error'" in browser_mod.PAGE_ERROR_COLLECTOR
    assert "addEventListener('unhandledrejection'" in browser_mod.PAGE_ERROR_COLLECTOR
    assert "console" not in browser_mod.PAGE_ERROR_COLLECTOR  # console stays untouched


def test_typing_plan_uses_keys_for_short_ascii_and_insert_otherwise(monkeypatch):
    monkeypatch.delenv("JEV_TYPE_MODE", raising=False)
    assert browser_mod.typing_plan("San Francisco") == ("keys", "San Francisco")
    assert browser_mod.typing_plan("") == ("insert", "")
    assert browser_mod.typing_plan("Zürich") == ("insert", "Zürich")  # non-ASCII goes through insert
    assert browser_mod.typing_plan("a\nb") == ("insert", "a\nb")
    assert browser_mod.typing_plan("x" * (browser_mod.TYPE_KEY_LIMIT + 1))[0] == "insert"
    monkeypatch.setenv("JEV_TYPE_MODE", "insert")
    assert browser_mod.typing_plan("short") == ("insert", "short")


def test_typing_gaps_burst_inside_words_and_pause_at_word_onsets():
    text = "Zurich 42"
    gaps = browser_mod.typing_gaps(text)
    assert len(gaps) == len(text) - 1
    # The bands are disjoint so a page measuring keystroke rhythm sees word-shaped cadence.
    for char, gap in zip(text[:-1], gaps):
        if char == " ":
            assert 0.035 <= gap <= 0.075, gap  # a new word starts slower
        elif char.isalnum() and char.isascii():
            assert 0.004 <= gap <= 0.012, gap  # bursts inside a word
        else:
            assert 0.018 <= gap <= 0.045, gap  # punctuation or symbol
    assert browser_mod.typing_gaps("a") == []


def test_char_key_params_match_a_real_keyboard():
    lower = browser_mod.char_key_params("a")
    assert lower == {"key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65,
                     "modifiers": 0, "text": "a", "unmodifiedText": "a"}
    upper = browser_mod.char_key_params("A")
    assert upper["modifiers"] == 8 and upper["code"] == "KeyA" and upper["text"] == "A"
    up = browser_mod.char_key_params("a", press=False)
    assert "text" not in up and "unmodifiedText" not in up
    assert browser_mod.char_key_params("5")["code"] == "Digit5"
    # Space is printable: without text the key press would insert nothing.
    assert browser_mod.char_key_params(" ")["text"] == " "


def test_arrow_params_target_the_direction_keys():
    down = browser_mod.arrow_params("Down")
    assert down == {"key": "ArrowDown", "code": "ArrowDown", "windowsVirtualKeyCode": 40}
    assert browser_mod.arrow_params("Up")["windowsVirtualKeyCode"] == 38


def test_wheel_plan_ticks_in_notches_and_lands_on_distance():
    for seed in range(40):
        plan = browser_mod.wheel_plan(560, rng=random.Random(seed))
        assert 4 <= len(plan) <= 7  # 4-6 ticks plus at most one ease-back
        assert all(28 <= abs(notch) <= 230 for notch in plan), plan
        assert all(notch > 0 for notch in plan[:-1])
        assert 470 <= sum(plan) <= 560  # the target distance, minus any ease-back
    down = browser_mod.wheel_plan(-560, rng=random.Random(3))
    assert all(notch < 0 for notch in down[:-1])
    assert browser_mod.wheel_plan(0) == []


def test_cursor_path_approaches_the_target_and_lands_on_it():
    import math
    points = browser_mod.path_points(100, 100, 700, 400, rng=random.Random(7))
    assert 3 <= len(points) <= 8
    assert points[-1] == (700, 400)
    distances = [math.hypot(x - 700, y - 400) for x, y in points]
    assert distances == sorted(distances, reverse=True)  # decelerating approach, no wandering back
    assert browser_mod.path_points(100, 100, 120, 110, rng=random.Random(7)) == [(120, 110)]
    assert browser_mod.path_points(100, 100, 100.5, 100, rng=random.Random(7)) == []


def test_move_cursor_travels_from_the_remembered_position(monkeypatch):
    monkeypatch.setattr(browser_mod.time, "sleep", lambda _seconds: None)
    browser_mod._session_state.clear()
    sent = []
    browser_mod._session_state["s9"] = {"cursor": (200, 200), "viewport": (1120, 780)}
    browser_mod.move_cursor(lambda method, **params: sent.append(params), "s9", 260, 260)
    assert all(params["type"] == "mouseMoved" for params in sent)
    assert sent[-1] == {"type": "mouseMoved", "x": 260, "y": 260}
    assert browser_mod._session_state["s9"]["cursor"] == (260, 260)
    sent.clear()
    browser_mod.move_cursor(lambda method, **params: sent.append(params), "s9", 260, 260)
    assert sent == []  # already resting there; a hand sends no event


def test_scroll_point_varies_inside_the_live_viewport():
    browser_mod._session_state.clear()
    points = {browser_mod.scroll_point("s1") for _ in range(200)}
    assert len(points) > 50  # not a fixed coordinate
    for x, y in points:
        assert 0 < x < 1120 and 0 < y < 780
    browser_mod._session_state["s2"] = {"viewport": (1366, 768)}
    x, y = browser_mod.scroll_point("s2")
    assert 1366 // 5 <= x <= 1366 * 4 // 5 and 768 // 5 <= y <= 768 * 7 // 10


def test_viewport_varies_per_run_and_pins_for_recordings(monkeypatch):
    monkeypatch.delenv("JEV_VIEWPORT", raising=False)
    sizes = {browser_mod.viewport_for_run() for _ in range(60)}
    assert len(sizes) >= 3  # not one fixed window forever
    monkeypatch.setenv("JEV_VIEWPORT", "1280x800")
    assert browser_mod.viewport_for_run() == (1280, 800)
    monkeypatch.setenv("JEV_VIEWPORT", "1280")
    with pytest.raises(ValueError, match="JEV_VIEWPORT"):
        browser_mod.viewport_for_run()
    monkeypatch.setenv("JEV_VIEWPORT", "300x300")
    with pytest.raises(ValueError, match="JEV_VIEWPORT"):
        browser_mod.viewport_for_run()
