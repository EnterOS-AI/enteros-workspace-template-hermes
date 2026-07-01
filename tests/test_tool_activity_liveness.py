"""RC #203 (tier-C liveness): the executor bumps ``MOLECULE_TOOL_ACTIVITY_FILE``
on each tool call so the base runtime's turn-lease watcher keeps the lease fresh
through a long tool-running turn — instead of falling to the coarse tier-D
output-liveness fallback. The hook is a strict no-op when the env var is unset
(off-kernel / older base image), so it is additive and byte-identical off-kernel.
"""
from __future__ import annotations

import pytest

from executor import HermesAgentProxyExecutor, _record_tool_activity


def test_record_tool_activity_noop_when_env_unset(tmp_path, monkeypatch):
    """Env var unset -> no file is created and nothing raises (off-kernel)."""
    monkeypatch.delenv("MOLECULE_TOOL_ACTIVITY_FILE", raising=False)
    _record_tool_activity()
    assert list(tmp_path.iterdir()) == []


def test_record_tool_activity_touches_file_when_env_set(tmp_path, monkeypatch):
    """Env var set -> the file is created and its mtime keeps advancing (the
    signal the parent turn-lease watcher reads)."""
    path = tmp_path / "activity"
    monkeypatch.setenv("MOLECULE_TOOL_ACTIVITY_FILE", str(path))
    assert not path.exists()
    _record_tool_activity()
    assert path.exists()
    first = path.stat().st_mtime_ns
    _record_tool_activity()
    assert path.stat().st_mtime_ns >= first


def test_record_tool_activity_survives_bad_path(tmp_path, monkeypatch):
    """A liveness ping must never break a tool call: a bad path is swallowed."""
    monkeypatch.setenv(
        "MOLECULE_TOOL_ACTIVITY_FILE", str(tmp_path / "no_such_dir" / "activity")
    )
    _record_tool_activity()  # must not raise


@pytest.mark.asyncio
async def test_dispatch_tool_bumps_activity_file(tmp_path, monkeypatch):
    """The per-tool hook fires on the real ``_dispatch_tool`` path — the touch
    precedes the dispatch, so it fires even when the molecule tool backend is
    unavailable in the test environment."""
    path = tmp_path / "activity"
    monkeypatch.setenv("MOLECULE_TOOL_ACTIVITY_FILE", str(path))
    # _dispatch_tool uses no instance attributes; bypass __init__.
    ex = object.__new__(HermesAgentProxyExecutor)
    tc = {"id": "call_1", "function": {"name": "list_peers", "arguments": "{}"}}
    await ex._dispatch_tool(tc)
    assert path.exists(), "a tool dispatch must bump the tier-C activity file"


def test_dispatch_tool_noop_off_kernel(tmp_path, monkeypatch):
    """Off-kernel (env unset), a tool dispatch writes no activity file."""
    import asyncio

    monkeypatch.delenv("MOLECULE_TOOL_ACTIVITY_FILE", raising=False)
    ex = object.__new__(HermesAgentProxyExecutor)
    tc = {"id": "call_1", "function": {"name": "list_peers", "arguments": "{}"}}
    asyncio.run(ex._dispatch_tool(tc))
    assert list(tmp_path.iterdir()) == []
