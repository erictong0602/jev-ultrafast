"""Real-browser verification of the browser-likeness handling.

Boots the actual Browser over Browser Harness and drives an instrumented local page
through fill / select / click / scroll exactly as the agent loop would. No model calls,
no external websites. What it proves, from the page's point of view:

  - every input event the page receives is a trusted browser event (isTrusted)
  - the cursor travels a multi-point path before a click, ending on the target
  - typed text arrives per character with a word-shaped rhythm (bursts, slower onsets)
  - the wheel flicks in notched ticks with a decaying tail, never one giant jump
  - the native select commits through keyboard-originated input/change events
  - no fixed automation global: __jevFast is gone, the cache key is non-enumerable
    and carries no scannable prefix even under full window enumeration
  - exceptions are still captured without Runtime.enable being enabled
  - navigator.webdriver is false, and the window size is a plausible per-run choice
"""

import time
from urllib.parse import quote

import jev_ultrafast.browser as browser_mod
from jev_ultrafast.browser import Browser

HTML = """<!doctype html><title>Browser-likeness verification</title>
<style>body{margin:30px}button{width:180px;height:50px}#spacer{height:3000px}</style>
<button id="buy" type="button">Buy</button>
<label>City <input id="city" value=""></label>
<select id="category" aria-label="Category">
  <option>All</option><option disabled>Hidden</option><option>Design</option><option>Coastal</option>
</select>
<div id="spacer"></div>
<script>
window.__ev = [];
window.__moves = [];
['mousemove','mousedown','mouseup','click','keydown','keyup','input','change'].forEach(t =>
  document.addEventListener(t, e => {
    const el = e.target;
    if (!['buy','city','category'].includes(el.id)) return;
    window.__ev.push({el: el.id, type: e.type, trusted: e.isTrusted,
                      key: e.key || '', x: e.clientX, y: e.clientY,
                      t: Math.round(performance.now())});
  }, true));
// Window-level travel log: intermediate path points hover over other elements,
// so per-element filters only see the landing move.
document.addEventListener('mousemove', e => {
  window.__moves.push([e.clientX, e.clientY, e.isTrusted, Math.round(performance.now())]);
}, true);
document.addEventListener('wheel', e => {
  window.__ev.push({el: 'wheel', type: 'wheel', trusted: e.isTrusted, dy: e.deltaY,
                    x: e.clientX, y: e.clientY, t: Math.round(performance.now())});
}, true);
</script>"""

TYPED = "Zurich 42"


def events_for(browser, element_id):
    return browser.evaluate(f"window.__ev.filter(e => e.el === {element_id!r})")


def main():
    # Record the CDP methods the attach path really uses.
    methods = []
    original_cdp = browser_mod.cdp

    def recording_cdp(method, **params):
        methods.append(method)
        return original_cdp(method, **params)

    browser_mod.cdp = recording_cdp
    browser = Browser("data:text/html," + quote(HTML))
    passed = []
    try:
        assert "Runtime.enable" not in methods, "Runtime.enable must stay off in the default path"
        assert "Page.addScriptToEvaluateOnNewDocument" in methods
        passed.append("attaches without Runtime.enable; passive collector is installed")

        assert browser.evaluate("navigator.webdriver") is False
        passed.append("navigator.webdriver is false, as in a plain Chrome")

        page = browser.observe(screenshot=False)
        viewport = browser_mod._session_state[browser.session]["viewport"]
        assert (page["w"], page["h"]) == viewport, (page["w"], page["h"], viewport)
        passed.append(f"emulated window is a plausible per-run size: {page['w']}x{page['h']}")

        fill = next(a for a in page["actions"] if a["kind"] == "fill")
        center = browser.evaluate(
            "(() => { const r = document.querySelector('#city').getBoundingClientRect();"
            " return [r.x + r.width / 2, r.y + r.height / 2]; })()")
        browser.act(fill, page, text=TYPED)
        travel = browser.evaluate("window.__moves")
        assert all(trusted for _, _, trusted, _ in travel), travel
        assert len({(x, y) for x, y, _, _ in travel}) >= 2, travel  # travel, not teleport
        assert abs(travel[-1][0] - center[0]) <= 1 and abs(travel[-1][1] - center[1]) <= 1
        value = browser.evaluate("document.querySelector('#city').value")
        assert value == TYPED, f"typed value wrong: {value!r}"
        events = events_for(browser, "city")
        assert all(e["trusted"] for e in events), f"untrusted event on the field: {events}"
        # One select-all press plus one keydown per typed character: per-character typing.
        keydowns = [e for e in events if e["type"] == "keydown"]
        assert len(keydowns) == len(TYPED) + 1, f"expected per-char keydowns, got {keydowns}"
        assert sum(e["type"] == "input" for e in events) == len(TYPED)
        stamps = [e["t"] for e in keydowns]
        bursts = [stamps[i + 1] - stamps[i] for i in (1, 2, 3, 4, 5, 6, 8)]
        onset = stamps[8] - stamps[7]  # starting the second word, after the space
        # CDP round-trip overhead rides on every gap, so the page-visible property is
        # relational: word onsets are slower than the bursts, and bursts are not uniform.
        assert onset > max(bursts), (bursts, onset)
        assert len(set(bursts)) >= max(3, len(bursts) // 2), bursts
        passed.append(f"typed {TYPED!r} per character with word-shaped rhythm; every event isTrusted")

        page = browser.observe(screenshot=False)
        select = next(a for a in page["actions"] if a["kind"] == "select" and a["value"] == "Design")
        browser.act(select, page)
        assert browser.evaluate("document.querySelector('#category').value") == "Design"
        events = events_for(browser, "category")
        changes = [e for e in events if e["type"] == "change"]
        assert changes and all(e["trusted"] for e in changes), f"select change not trusted: {events}"
        arrows = [e for e in events if e["type"] == "keydown"]
        assert arrows and all(e["key"] == "ArrowDown" and e["trusted"] for e in arrows), arrows
        passed.append("native select committed via trusted ArrowDown; change is trusted")

        page = browser.observe(screenshot=False)
        click = next(a for a in page["actions"] if a["kind"] == "click" and a["label"] == "Buy")
        rect = browser.evaluate(
            "(() => { const r = document.querySelector('#buy').getBoundingClientRect();"
            " return [r.x + r.width / 2, r.y + r.height / 2]; })()")
        moves_before = browser.evaluate("window.__moves.length")
        browser.act(click, page)
        travel = browser.evaluate("window.__moves")[moves_before:]
        assert travel and all(trusted for _, _, trusted, _ in travel), travel
        assert len({(x, y) for x, y, _, _ in travel}) >= 2, travel  # travel, not teleport
        center_x, center_y = rect
        assert abs(travel[-1][0] - center_x) <= 1 and abs(travel[-1][1] - center_y) <= 1, (travel[-1], rect)
        assert browser.evaluate("window.__ev.filter(e => e.el === 'buy' && e.type === 'click').length") == 1
        events = events_for(browser, "buy")
        order = [e["type"] for e in events]
        assert order[-3:] == ["mousedown", "mouseup", "click"], order
        assert all(e["trusted"] for e in events)
        down_at, up_at = events[-3]["t"], events[-2]["t"]
        assert 5 <= up_at - down_at <= 120, "press and release are not instantaneous"
        passed.append(f"click = {len(travel)}-point travel ending on target, trusted, paced press")

        page = browser.observe(screenshot=False)
        scroll = next(a for a in page["actions"] if a["id"] == "scroll_down")
        top_before = browser.evaluate("window.scrollY")
        browser.act(scroll, page)
        time.sleep(1.0)  # Chrome animates the flick; let the smooth scroll land
        wheels = events_for(browser, "wheel")
        assert 4 <= len(wheels) <= 7, wheels  # notched ticks, never one giant jump
        assert all(e["trusted"] for e in wheels), wheels
        forwards = [e for e in wheels if e["dy"] > 0]
        assert len(forwards) >= 3 and len(wheels) - len(forwards) <= 1, wheels  # at most one ease-back
        assert len({(e["x"], e["y"]) for e in wheels}) >= 3, wheels  # the hand trembles between ticks
        landed = browser.evaluate("window.scrollY") - top_before
        assert 400 <= landed <= 570, landed
        passed.append(f"wheel flick = {len(wheels)} trusted notched ticks, landing {landed:.0f}px")

        # The page can enumerate window, but finds neither the old global nor a scannable key.
        assert browser.evaluate("typeof window.__jevFast") == "undefined"
        descriptor = browser.evaluate(
            f"(() => {{ const d = Object.getOwnPropertyDescriptor(window, {browser_mod.FAST_KEY!r});"
            " return d ? {enumerable: d.enumerable, configurable: d.configurable} : null; })()")
        assert descriptor == {"enumerable": False, "configurable": True}, descriptor
        suspicious = browser.evaluate(
            "Object.getOwnPropertyNames(window).filter(n => "
            "/jev|webdriver|cdc|selenium|puppeteer|playwright/i.test(n))")
        assert suspicious == [], suspicious
        passed.append("window enumeration reveals no tool name, no prefix, non-enumerable cache key")

        browser.evaluate("setTimeout(() => { throw new Error('collector-boom'); }, 0)")
        time.sleep(0.4)
        browser.observe(screenshot=False)
        summary = browser.error_summary()
        assert any("collector-boom" in item["text"] for item in summary["items"]), summary
        passed.append("uncaught exception captured by the passive collector, no Runtime domain")

        print("\n".join(f"PASS: {line}" for line in passed))
        print(f"PASS: {len(passed)} browser-likeness checks on a live Chrome; no model calls")
    finally:
        browser_mod.cdp = original_cdp
        browser.close()


if __name__ == "__main__":
    main()
