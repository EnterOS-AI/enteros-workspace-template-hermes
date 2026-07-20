"""Tool-trace capture (canvas chain parity with claude-code).

The executor snapshots /tmp/.hermes/sessions/*.jsonl before a turn and maps
the post-reply delta's assistant tool_calls into the platform tool_trace
shape ([{"tool", "input"?}]). These tests pin the delta parser against the
real session record shape (role=assistant + tool_calls[].function).
"""
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_executor(monkeypatch, tmp_path):
    import executor as ex
    monkeypatch.setattr(ex, "_SESSIONS_DIR", str(tmp_path))
    return ex


def test_delta_maps_tool_calls(monkeypatch, tmp_path):
    ex = _load_executor(monkeypatch, tmp_path)
    f = tmp_path / "s1.jsonl"
    pre = json.dumps({"role": "assistant", "tool_calls": [
        {"function": {"name": "old_tool", "arguments": "{}"}}]}) + "\n"
    f.write_text(pre, encoding="utf-8")
    snap = ex._session_jsonl_snapshot()

    rows = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "mcp_molecule_list_peers", "arguments": '{"q":1}'}},
            {"function": {"name": "", "arguments": "ignored"}},
        ]},
        {"role": "tool", "content": "result"},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "create_workspace", "arguments": "x" * 500}}]},
    ]
    with open(f, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    trace = ex._tool_trace_from_session_delta(snap)
    assert [t["tool"] for t in trace] == ["mcp_molecule_list_peers", "create_workspace"]
    assert trace[0]["input"] == '{"q":1}'
    # input truncation cap
    assert len(trace[1]["input"]) == ex._TOOL_TRACE_INPUT_MAX
    # pre-snapshot rows (old_tool) are NOT in the delta
    assert all(t["tool"] != "old_tool" for t in trace)


def test_delta_tolerates_garbage_and_missing_dir(monkeypatch, tmp_path):
    ex = _load_executor(monkeypatch, tmp_path)
    f = tmp_path / "s2.jsonl"
    f.write_text("", encoding="utf-8")
    snap = ex._session_jsonl_snapshot()
    with open(f, "a", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write(json.dumps({"role": "assistant"}) + "\n")
    assert ex._tool_trace_from_session_delta(snap) == []

    monkeypatch.setattr(ex, "_SESSIONS_DIR", str(tmp_path / "nope"))
    assert ex._session_jsonl_snapshot() == {}
    assert ex._tool_trace_from_session_delta({}) == []
