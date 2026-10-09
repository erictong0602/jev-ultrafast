"""Offline contracts for multi-agent coordination: naming, origin locks, shared results."""

import json
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

import jev_ultrafast.browser as browser_mod
import jev_ultrafast.coordination as coord
from jev_ultrafast.browser import Browser, OriginBusy, OriginLock
from jev_ultrafast.reporting import build_record


@pytest.fixture
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_LOCKS_DIR", str(tmp_path / "locks"))
    return tmp_path / "locks"


def test_agent_name_prefers_jev_name_then_bu_name(monkeypatch):
    monkeypatch.delenv("JEV_AGENT_NAME", raising=False)
    monkeypatch.delenv("BU_NAME", raising=False)
    assert coord.agent_name() == "default"
    monkeypatch.setenv("BU_NAME", "shell-agent")
    assert coord.agent_name() == "shell-agent"
    monkeypatch.setenv("JEV_AGENT_NAME", "worker-2")
    assert coord.agent_name() == "worker-2"


def test_origin_lock_is_exclusive_and_released(lock_dir):
    first = OriginLock("example.test")
    assert first.acquire() and lock_dir.exists()
    second = OriginLock("example.test")
    assert second.acquire(wait_s=0.05) is False  # Held: a second agent is refused.
    first.release()
    assert second.acquire() is True  # Released: the site is free again.
    second.release()


def test_contested_knows_our_own_lock_from_a_foreign_one(lock_dir, monkeypatch):
    mine = OriginLock("app.test")
    mine.acquire()
    assert OriginLock.contested("app.test") is False  # Our own lock never blocks healing.
    # A foreign holder is another process: the file is locked but absent from
    # this process's registry. Forget our registration to simulate one.
    monkeypatch.delitem(OriginLock._held, "app.test")
    assert OriginLock.contested("app.test") is True  # Held by someone else: the probe decides.
    mine.release()
    assert OriginLock.contested("app.test") is False  # Released: the site is free.


def test_browser_refuses_a_held_origin_before_touching_the_daemon(lock_dir, monkeypatch):
    holder = OriginLock("busy.test")
    assert holder.acquire()
    opened = []
    monkeypatch.setattr(browser_mod, "ensure_isolated_daemon", lambda: opened.append(1))
    monkeypatch.setattr(browser_mod, "cdp", Mock(side_effect=AssertionError("no tab on a busy origin")))
    with pytest.raises(OriginBusy, match="busy.test"):
        Browser("https://busy.test/path", collect_errors=False, heal_session=False)
    assert opened == []  # Refusal happens before any daemon or tab work.
    holder.release()


def test_a_failed_browser_init_releases_its_origin_lock(lock_dir, monkeypatch):
    def boom():
        raise RuntimeError("daemon down")

    monkeypatch.setattr(browser_mod, "ensure_isolated_daemon", boom)
    with pytest.raises(RuntimeError):
        Browser("https://crashy.test/", collect_errors=False, heal_session=False)
    assert OriginLock.contested("crashy.test") is False  # The lock died with the init.


def test_heal_keeps_cookies_when_another_agent_holds_the_origin(lock_dir, monkeypatch):
    b = Browser.__new__(Browser)
    b.document_status = 401
    b.document_url = "https://shared.test/x"
    b.session_events = []
    b._load = Mock()
    b.cookies = Mock(return_value=[{"name": "sid"}])
    b.clear_cookies = Mock()
    b.clear_cache = Mock()
    monkeypatch.setattr(browser_mod.OriginLock, "contested", staticmethod(lambda host: True))
    b.heal("https://shared.test/x")
    b.clear_cookies.assert_not_called()
    b.clear_cache.assert_not_called()
    b._load.assert_called_once()  # The pre-clear reload happened; nothing was destroyed.
    assert b.session_events == ["landing looks logged out", "another agent holds this origin; cookies kept"]


def test_shared_results_file_keeps_every_line_intact(tmp_path):
    path = tmp_path / "results.jsonl"

    def append(worker):
        for i in range(20):
            coord.append_record(path, {"worker": worker, "i": i})

    threads = [threading.Thread(target=append, args=(w,)) for w in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 120
    assert sorted((r["worker"], r["i"]) for r in rows) == sorted(
        (w, i) for w in range(6) for i in range(20)
    )


def test_records_carry_the_producing_agent_when_named():
    plain = build_record(url="https://x/", status="done")
    assert "agent" not in plain
    named = build_record(url="https://x/", status="done", agent="jev-2")
    assert named["agent"] == "jev-2"


def test_demo_port_is_stable_per_agent(monkeypatch):
    import jev_ultrafast.demo as demo

    monkeypatch.delenv("TYPESAFE_DEMO_PORT", raising=False)
    monkeypatch.delenv("JEV_AGENT_NAME", raising=False)
    monkeypatch.delenv("BU_NAME", raising=False)
    assert demo._demo_port() == 8766
    monkeypatch.setenv("JEV_AGENT_NAME", "jev-1")
    first = demo._demo_port()
    assert first == demo._demo_port() and 8766 <= first < 8866
    monkeypatch.setenv("TYPESAFE_DEMO_PORT", "9001")
    assert demo._demo_port() == 9001  # Explicit override always wins.


def test_worker_commands_carry_their_task_and_identity(tmp_path):
    import jev_ultrafast.orchestrate as orch

    tasks = orch.parse_tasks(str(Path("jev_ultrafast") / "questions.py"))  # no tasks: file is code
    assert tasks == []
    task_file = tmp_path / "tasks.jsonl"
    task_file.write_text(
        json.dumps({"url": "https://a.test/", "goals": ["One", "Two"], "expect-text": "A", "keep-open": True})
        + "\n"
        + "\n"
        + json.dumps({"url": "https://b.test/", "goals": ["Go"], "origin-wait": 30})
        + "\n",
        encoding="utf-8",
    )
    parsed = orch.parse_tasks(str(task_file))
    assert [number for number, _ in parsed] == [1, 3]
    runner = Path("examples/run.py")
    cmd = orch.worker_command(runner, parsed[0][1], jsonl="out.jsonl", origin_wait=0)
    assert cmd[:5] == [orch.sys.executable, str(runner), "--url", "https://a.test/", "--goal"]
    assert "Two" in cmd and "--expect-text" in cmd and "A" in cmd
    assert "--keep-open" in cmd and "--jsonl" in cmd
    wait_at = cmd.index("--origin-wait")
    assert cmd[wait_at + 1] == "0"  # Coordinator default, task did not override.
    cmd_b = orch.worker_command(runner, parsed[1][1], jsonl=None, origin_wait=5)
    assert "--origin-wait" in cmd_b and cmd_b[cmd_b.index("--origin-wait") + 1] == "30"


def test_runner_resolves_from_checkout_or_env(tmp_path, monkeypatch):
    import jev_ultrafast.orchestrate as orch

    checkout = tmp_path / "checkout"
    (checkout / "examples").mkdir(parents=True)
    (checkout / "examples" / "run.py").write_text("# runner\n", encoding="utf-8")
    monkeypatch.chdir(checkout)
    monkeypatch.delenv("JEV_CHECKOUT", raising=False)
    assert orch.find_runner() == checkout / "examples" / "run.py"
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "examples").mkdir(parents=True)
    (elsewhere / "examples" / "run.py").write_text("# runner\n", encoding="utf-8")
    monkeypatch.setenv("JEV_CHECKOUT", str(elsewhere))
    assert orch.find_runner() == elsewhere / "examples" / "run.py"  # Env wins over cwd.
    monkeypatch.setenv("JEV_CHECKOUT", str(tmp_path / "empty"))
    with pytest.raises(SystemExit, match="JEV_CHECKOUT"):
        orch.find_runner()


def test_dry_run_plans_without_spawning(tmp_path, capsys):
    import jev_ultrafast.orchestrate as orch

    task_file = tmp_path / "tasks.jsonl"
    task_file.write_text(json.dumps({"url": "https://a.test/", "goals": ["Look"]}), encoding="utf-8")
    rows = orch.run_queue(
        orch.parse_tasks(str(task_file)), jobs=2, jsonl=None, origin_wait=7, dry_run=True
    )
    assert rows == [(1, 0)]
    out = capsys.readouterr().out
    assert "BU_NAME=jev-1" in out and "--origin-wait" in out and "7" in out
