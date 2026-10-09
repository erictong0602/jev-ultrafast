"""Shared offline-test isolation: no test ever locks a real origin."""

import pytest


@pytest.fixture(autouse=True)
def lock_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_LOCKS_DIR", str(tmp_path / "locks"))
