"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import os
import random
import time
from pathlib import Path

from .browser import Browser, CoveredTarget, StalePage
from .model import action_space, choose, field_context, field_text
from .questions import MAX_STEPS


class BudgetExhausted(ValueError):
    """The run reached its configured decision or action budget."""

class Stalled(ValueError):
    """Consecutive decisions executed no action; the run made no progress."""


# A control an overlay covers cannot be clicked until the overlay clears, so
# deciding again against the same page only spends billed calls. Fail early on
# that specific refusal, with the covering element named in the stop reason.
COVERED_STALL_LIMIT = 6


# A freshly opened SPA tab can sit on a blank app shell (no text, no controls) while it
# boots. Settling is free; a decision against the shell is a billed call the model can
# only answer BLOCKED to.
BLANK_SETTLE_S = 5.0
BLANK_POLL_S = 0.25


def _has_content(page):
    return bool(page.get("text")) or any(a["kind"] not in {"scroll", "wait"} for a in page["actions"])


def _dwell(state):
    """Opt-in reading pause before an action, JEV_DWELL_MS='min' or 'min-max' in
    milliseconds. Off by default: the loop's speed is the product, so a human
    cadence is something the operator chooses, with a longer pause after a page change."""
    spec = os.environ.get("JEV_DWELL_MS", "").strip()
    if not spec:
        return
    try:
        bounds = sorted(float(part) for part in spec.replace("-", " ").split())
    except ValueError:
        raise ValueError("JEV_DWELL_MS is 'min' or 'min-max' in milliseconds, e.g. 300-1200") from None
    if len(bounds) > 2 or bounds[0] < 0:
        raise ValueError("JEV_DWELL_MS is 'min' or 'min-max' in milliseconds, e.g. 300-1200")
    high = bounds[-1]
    low = bounds[0]
    changed = bool(state["history"] and state["history"][-1].get("page_changed"))
    time.sleep(random.uniform(low, high) / 1000 * (1.5 if changed else 1.0))


class Agent:
    def __init__(self, url, goals, *, record_dir=None, screenshots=False, foreground=False,
                 keep_open=False, collect_errors=True, heal_session=True, files=None,
                 max_actions=MAX_STEPS, max_decisions=MAX_STEPS * 2, max_stall=12, choose_retries=2,
                 max_covered=COVERED_STALL_LIMIT, origin_wait=0):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        self.pending_text = None
        # SET_FILE attachments come from the operator; the model never names paths.
        self.files = [str(Path(f).resolve()) for f in (files or [])]
        for path in self.files:
            if not Path(path).is_file():
                raise ValueError(f"File to upload does not exist: {path}")
        self.browser = Browser(url, foreground=foreground, keep_open=keep_open,
                               collect_errors=collect_errors, heal_session=heal_session,
                               origin_wait=origin_wait)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
            files=self.files,
            max_actions=int(max_actions),
            max_decisions=int(max_decisions),
            max_stall=int(max_stall),
            max_covered=int(max_covered),
            choose_retries=int(choose_retries),
            stalled_ticks=0,
            covered_ticks=0,
            refusals=[],
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def _settle(self):
        """Let a blank observed page finish rendering before the next billed decision.
        Polling observes nothing while the page marker is unchanged; hydration or
        navigation changes it, and the fresh observation replaces the blank one."""
        page = self.state["page"]
        if _has_content(page):
            return
        deadline = time.monotonic() + BLANK_SETTLE_S
        while time.monotonic() < deadline:
            time.sleep(BLANK_POLL_S)
            if not self.state["browser"].fresh(page):
                page = self.state["browser"].observe(screenshot=self.screenshots)
                self.state["page"] = page
            if _has_content(page):
                break

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                result = self.command("act", {"fingerprint": state["page"]["fingerprint"]})
                state["stalled_ticks"] = 0
                return result
            except StalePage as refusal:
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                # Repeated decisions that execute nothing burn billed calls without progress.
                # The last few reasons ride along so a stall on record says why it stalled.
                reason = str(refusal)
                state["stalled_ticks"] = state.get("stalled_ticks", 0) + 1
                state["refusals"] = (state.get("refusals") or [])[-7:] + [reason]
                state["covered_ticks"] = (
                    state.get("covered_ticks", 0) + 1 if isinstance(refusal, CoveredTarget) else 0
                )
                if state["covered_ticks"] >= state.get("max_covered", COVERED_STALL_LIMIT):
                    state["status"] = "blocked"
                    raise Stalled(
                        f"No reachable target after {state['covered_ticks']} decisions: {reason}"
                    ) from None
                if state["stalled_ticks"] >= state.get("max_stall", 12):
                    state["status"] = "blocked"
                    raise Stalled(
                        f"No executed action for {state['stalled_ticks']} consecutive decisions: {reason}"
                    ) from None
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            self._settle()
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            if len(state["decisions"]) >= state.get("max_decisions", MAX_STEPS * 2):
                raise BudgetExhausted("Reached the demo's model-call budget")
            for attempt in range(state.get("choose_retries", 0) + 1):
                try:
                    state["decision"] = choose(state["page"], state["goal"], state["history"])
                    break
                except ValueError as error:
                    # An invalid sample is a transient provider fault; re-asking draws a new one.
                    if attempt >= state.get("choose_retries", 0) or "Invalid TypeSafe response" not in str(error):
                        raise
                    time.sleep(0.5 * (attempt + 1))
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= state.get("max_actions", MAX_STEPS):
                state["status"] = "blocked"
                raise BudgetExhausted(f"Stopped at the {state.get('max_actions', MAX_STEPS)}-action demo budget")
            text, helper = None, None
            if action["kind"] == "fill":
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    text, helper = field_text(context)
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
            # Browser.act checks freshness immediately before input, including after text generation.
            _dwell(state)
            state["browser"].act(action, page, text=text,
                                 files=state.get("files") if action["kind"] == "file" else None)
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            state["status"] = (
                "blocked"
                if len(repeated) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated)
                else "ready"
            )
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
