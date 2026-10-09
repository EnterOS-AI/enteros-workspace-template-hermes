"""Context-overflow auto-heal on the hermes plugin lane (issue #370).

When a hermes session outgrows the model's context window and compaction
gives up, every subsequent turn resumes the same oversized transcript and
dies identically — the workspace is permanently dead with no error visible
on the platform record (status stays online / wedged=false / error_rate=0).

hermes ships its own remedy in gateway/run.py (skip-persist + reset_session,
upstream #9893/#10063/#35809) but it does NOT fire on the molecule-a2a lane:
workspace 90139d37 logged 50 "Cannot compress further" turns on 2026-08-12/13
while state.db held only 2 session_reset rows, both operator-typed /new.

So the executor heals it: detect the overflow reply, clear the session with
hermes's own `/new`, and replay the turn ONCE on the fresh session.

These tests pin (a) the classifier — including the negatives that make it
safe to discard a session — and (b) the heal's control flow, especially that
it is bounded and cannot loop.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import executor as ex  # noqa: E402


# --- classifier -----------------------------------------------------------

# Strings hermes actually emits on a terminal overflow. Sourced from
# agent/conversation_loop.py in the pinned wheel (hermes-agent 0.19.0) and
# from the live gateway.log of workspace 90139d37.
_OVERFLOW_TEXTS = [
    "Context length exceeded (182,430 tokens). Cannot compress further.",
    "Context length exceeded (245,527 tokens). Cannot compress further.",
    "Context length exceeded: max compression attempts (3) reached.",
    "Request payload too large: max compression attempts (3) reached.",
    "Request payload too large (413). Cannot compress further.",
    # Case-insensitivity: the reply is prose, not a stable error code.
    "CONTEXT LENGTH EXCEEDED (205,130 TOKENS). CANNOT COMPRESS FURTHER.",
]

# Failures that must NOT be treated as overflow. Resetting on any of these
# would discard a healthy transcript — the conversation is recoverable, the
# session is not oversized, and the user would silently lose history.
_NON_OVERFLOW_TEXTS = [
    "Rate limit exceeded. Please retry in 30s.",
    "429 Too Many Requests",
    "invalid auth token",
    "Provider returned 503 Service Unavailable",
    "The model produced an empty response.",
    "I read the file and found 3 functions exceeding the limit.",
    "",
    None,
]


@pytest.mark.parametrize("text", _OVERFLOW_TEXTS)
def test_classifier_matches_overflow_strings(text):
    assert ex._is_context_overflow(text) is True


@pytest.mark.parametrize("text", _NON_OVERFLOW_TEXTS)
def test_classifier_rejects_non_overflow(text):
    assert ex._is_context_overflow(text) is False


# Pinned LITERALLY, not read from the module under test: a parametrize over
# ex._CONTEXT_OVERFLOW_PATTERNS deletes its own case when a pattern is
# deleted, so the suite stays green through exactly the regression it is
# meant to catch (verified — it did).
_REQUIRED_PHRASES = [
    "cannot compress further",
    "context length exceeded",
    "max compression attempts",
    "request payload too large",
]


def test_no_required_phrase_was_dropped():
    """The pattern tuple must still carry every phrase we rely on."""
    missing = [p for p in _REQUIRED_PHRASES
               if p not in ex._CONTEXT_OVERFLOW_PATTERNS]
    assert not missing, f"overflow patterns dropped: {missing}"


@pytest.mark.parametrize("pattern", _REQUIRED_PHRASES)
def test_every_pattern_is_load_bearing(pattern):
    """Each pattern must independently classify its own branch's wording.

    The real hermes strings overlap — "Context length exceeded (N tokens).
    Cannot compress further." matches two patterns at once — so asserting
    only on whole strings lets a pattern be deleted with every test still
    green (verified: removing "cannot compress further" broke nothing).
    This pins each pattern individually, so dropping any one fails here.

    Each phrase guards a DIFFERENT upstream branch in
    agent/conversation_loop.py (the 413 pair, the compression-attempt
    ceiling, the terminal minimum-tier branch); their wordings can change
    independently, so the redundancy is deliberate and must be preserved.
    """
    assert ex._is_context_overflow(f"hermes turn failed: {pattern}") is True


def test_patterns_are_multiword_phrases():
    """Guard the 'deliberately NARROW' property.

    A bare token like "exceeded" or "token" would match rate-limit and auth
    errors, and the heal DISCARDS a session — a false positive destroys real
    conversation history. Every pattern must be a multi-word phrase.
    """
    for pattern in ex._CONTEXT_OVERFLOW_PATTERNS:
        assert " " in pattern, f"pattern {pattern!r} is too broad to be safe"
        assert pattern == pattern.lower(), f"pattern {pattern!r} must be lowercase"


# --- heal control flow ----------------------------------------------------

class _FakeQueue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


def _executor_with_scripted_replies(monkeypatch, replies):
    """Build an executor whose _dispatch_and_wait returns `replies` in order.

    Records every dispatched `content` so the test can assert what was sent
    (the turn prompt vs the `/new` reset) and how many turns ran, plus the
    `source_type` on each dispatch so the retry's provenance can be checked.
    """
    inst = ex.HermesAgentProxyExecutor.__new__(ex.HermesAgentProxyExecutor)
    sent = []
    source_types = []
    pending = list(replies)

    async def fake_dispatch(self, *, content, chat_id, peer_id, peer_name,
                            callback_url, history, event_queue,
                            source_type=""):
        sent.append(content)
        source_types.append(source_type)
        return pending.pop(0) if pending else None

    monkeypatch.setattr(
        ex.HermesAgentProxyExecutor, "_dispatch_and_wait", fake_dispatch
    )
    monkeypatch.setattr(
        ex.HermesAgentProxyExecutor, "_derive_chat_id",
        lambda self, ctx: "chat-1",
    )
    monkeypatch.setattr(
        ex.HermesAgentProxyExecutor, "_derive_peer_identity",
        lambda self, ctx: ("", None),
    )
    monkeypatch.setattr(ex, "_session_jsonl_snapshot", lambda: {})
    monkeypatch.setattr(ex, "_tool_trace_from_session_delta", lambda snap: [])
    inst._callback_host = "127.0.0.1"
    inst._callback_port = 8646
    return inst, sent, source_types


def _run(inst, queue, prompt="do the work"):
    return asyncio.run(
        inst._execute_via_plugin(object(), queue, prompt, history=None)
    )


def test_overflow_triggers_reset_then_retry(monkeypatch):
    """The wedge-breaker: overflow -> /new -> replay the SAME prompt once."""
    overflow = "Context length exceeded (182,430 tokens). Cannot compress further."
    inst, sent, _source_types = _executor_with_scripted_replies(
        monkeypatch,
        [overflow, "Session reset! Starting fresh.", "here is the real answer"],
    )
    queue = _FakeQueue()
    _run(inst, queue)

    assert sent == ["do the work", "/new", "do the work"], (
        "expected the turn, then the reset, then a replay of the same turn"
    )
    assert len(queue.events) == 1, "exactly one reply is delivered to the caller"


def test_healthy_turn_does_not_reset(monkeypatch):
    """No reset on a normal reply — the heal must not touch healthy sessions."""
    inst, sent, _source_types = _executor_with_scripted_replies(monkeypatch, ["all done"])
    queue = _FakeQueue()
    _run(inst, queue)

    assert sent == ["do the work"]
    assert "/new" not in sent


def test_heal_is_bounded_to_one_retry(monkeypatch):
    """A second overflow on a FRESH session must not loop.

    If the prompt itself exceeds the window no reset can help, so the
    overflow text is delivered rather than resetting forever. Without this
    bound the executor would spin /new + retry until the turn timed out.
    """
    overflow = "Context length exceeded (245,527 tokens). Cannot compress further."
    inst, sent, _source_types = _executor_with_scripted_replies(
        monkeypatch, [overflow, "Session reset!", overflow],
    )
    queue = _FakeQueue()
    _run(inst, queue)

    assert sent.count("/new") == 1, "must reset at most once per turn"
    assert sent == ["do the work", "/new", "do the work"]
    assert len(queue.events) == 1


def test_failed_reset_skips_the_retry(monkeypatch):
    """If /new never comes back, do not replay onto the same bloated session.

    Retrying without a confirmed reset just burns another full turn against
    the transcript that is already known to overflow.
    """
    overflow = "Context length exceeded (182,430 tokens). Cannot compress further."
    inst, sent, _source_types = _executor_with_scripted_replies(
        monkeypatch, [overflow, None],
    )
    queue = _FakeQueue()
    _run(inst, queue)

    assert sent == ["do the work", "/new"], "no replay after a failed reset"
    assert len(queue.events) == 1, "the overflow text is still surfaced"


def test_reset_reply_that_is_itself_an_overflow_aborts(monkeypatch):
    """A `/new` that returns the overflow string means it was not cleared."""
    overflow = "Context length exceeded (182,430 tokens). Cannot compress further."
    inst, sent, _source_types = _executor_with_scripted_replies(
        monkeypatch, [overflow, overflow],
    )
    queue = _FakeQueue()
    _run(inst, queue)

    assert sent == ["do the work", "/new"], "abort when the reset did not take"


# ---- provenance across the heal -------------------------------------


class _CtxWithMetadata:
    """Minimal context carrying message.metadata, for _derive_source_type.

    Deliberately a plain class, not a MagicMock: a MagicMock auto-creates
    `.message` and every other attribute, which is exactly the trap that
    silently emptied the wire-level tests in test_executor_plugin_path.py.
    """

    class _Msg:
        def __init__(self, metadata):
            self.metadata = metadata

    def __init__(self, metadata):
        self.message = self._Msg(metadata)


def _run_with_ctx(inst, queue, ctx, prompt="do the work"):
    return asyncio.run(inst._execute_via_plugin(ctx, queue, prompt, history=None))


def test_retry_after_heal_keeps_scheduled_provenance(monkeypatch):
    """The auto-heal replay is the SAME turn and must not change provenance.

    If the retry dropped the marker, a scheduled run that happened to overflow
    once would silently become an interactive turn — and then block on an
    approval nobody is listening for. The heal must not be able to launder a
    turn's origin.
    """
    overflow = "Context length exceeded (182,430 tokens). Cannot compress further."
    inst, sent, source_types = _executor_with_scripted_replies(
        monkeypatch,
        [overflow, "Session reset! Starting fresh.", "here is the real answer"],
    )
    queue = _FakeQueue()
    _run_with_ctx(inst, queue, _CtxWithMetadata({"source_type": "self-scheduler"}))

    assert sent == ["do the work", "/new", "do the work"]
    # dispatch 0 = the turn, 1 = the internal /new reset, 2 = the replay.
    assert source_types[0] == "self-scheduler"
    assert source_types[2] == "self-scheduler", (
        "the heal replay lost the scheduled marker; the same turn changed "
        f"provenance mid-heal. source_types={source_types!r}"
    )


def test_interactive_turn_stays_interactive_across_the_heal(monkeypatch):
    """The mirror case: a human turn must never acquire a marker."""
    overflow = "Context length exceeded (182,430 tokens). Cannot compress further."
    inst, sent, source_types = _executor_with_scripted_replies(
        monkeypatch,
        [overflow, "Session reset! Starting fresh.", "here is the real answer"],
    )
    queue = _FakeQueue()
    _run_with_ctx(inst, queue, _CtxWithMetadata({"peer_id": "ws-1"}))

    assert sent == ["do the work", "/new", "do the work"]
    assert set(source_types) == {""}, (
        f"an interactive turn acquired a provenance marker: {source_types!r}"
    )
