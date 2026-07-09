"""Offline unit tests for the codex context-window + interrupt-mapping fixes.
No model calls — feeds synthetic notifications shaped exactly like the real ones
(captured from gpt-5.5: tokenUsage.{last,total,modelContextWindow})."""
import asyncio
from cc_remote.wrapper.codex_handle import CodexHandle
from cc_remote.wrapper.codex_stream import CodexStreamTranslator


class _Cfg:
    tool_result_max = 8000
    cc_cwd = "/tmp"


def test_context_window_capture_and_usage():
    h = CodexHandle(_Cfg())
    # before any turn: no server value, falls back to a config-declared window,
    # used is None (renders as 0) — never crashes.
    u0 = asyncio.run(h.get_context_usage())
    assert u0["context_window"] and u0["context_window"] > 0, u0
    assert u0["used_tokens"] is None, u0

    # real notification shape (verified live).
    notif = {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {
        "last":  {"totalTokens": 21246, "inputTokens": 21241, "cachedInputTokens": 4992,
                  "outputTokens": 5, "reasoningOutputTokens": 0},
        "total": {"totalTokens": 21246, "inputTokens": 21241, "cachedInputTokens": 4992,
                  "outputTokens": 5, "reasoningOutputTokens": 0},
        "modelContextWindow": 258400}}}
    asyncio.run(h._dispatch(notif))
    assert h.context_window == 258400, h.context_window
    u = asyncio.run(h.get_context_usage())
    assert u["used_tokens"] == 21246, u
    assert u["context_window"] == 258400, u
    pct = u["used_tokens"] / u["context_window"] * 100
    assert 8.0 < pct < 8.5, pct
    print(f"  context: used={u['used_tokens']} / {u['context_window']} = {pct:.1f}%  OK")


def test_context_uses_last_not_cumulative_total():
    """On a later turn, `total` is the cumulative session sum (over-counts context);
    the gauge must use `last` (current depth)."""
    h = CodexHandle(_Cfg())
    asyncio.run(h._dispatch({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {
        "last":  {"totalTokens": 40000},
        "total": {"totalTokens": 120000},   # 3 turns' cumulative
        "modelContextWindow": 258400}}}))
    u = asyncio.run(h.get_context_usage())
    assert u["used_tokens"] == 40000, u   # last, NOT 120000
    print(f"  context uses last(40000) not total(120000)  OK")


def test_interrupt_status_maps_to_cc_vocab():
    tr = CodexStreamTranslator(8000)
    evs = tr.feed({"method": "turn/completed", "params": {"turn": {"status": "interrupted", "durationMs": 3000}}})
    assert len(evs) == 1
    te = evs[0]
    assert te.result.subtype == "error_during_execution", te.result.subtype
    assert te.result.is_error is True
    print(f"  interrupted -> subtype={te.result.subtype} is_error={te.result.is_error}  OK")

    tr2 = CodexStreamTranslator(8000)
    ok = tr2.feed({"method": "turn/completed", "params": {"turn": {"status": "completed", "durationMs": 500}}})
    assert ok[0].result.subtype == "success" and ok[0].result.is_error is False
    print(f"  completed -> subtype=success is_error=False  OK")

    tr3 = CodexStreamTranslator(8000)
    fail = tr3.feed({"method": "turn/completed", "params": {"turn": {"status": "failed"}}})
    assert fail[0].result.subtype == "error" and fail[0].result.is_error is True
    print(f"  failed -> subtype=error is_error=True  OK")


def test_config_fast_toggle_preserves_file():
    """/fast edits ~/.codex/config.toml (top-level service_tier) without touching
    the rest of the file. Runs against a temp copy — never the real config."""
    import os, tempfile, cc_remote.wrapper.codex_sessions as cs
    src = ('model_provider = "cubence"\nmodel = "gpt-5.5"\n'
           'model_reasoning_effort = "xhigh"\nservice_tier = "fast"\n\n'
           '[model_providers.cubence]\nbase_url = "https://x/v1"\n')
    tf = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    tf.write(src); tf.close()
    orig = cs._CONFIG
    cs._CONFIG = tf.name
    try:
        assert cs.codex_fast_enabled() is True
        assert cs.set_codex_config_fast(False)
        body = open(tf.name).read()
        assert "service_tier" not in body and "[model_providers.cubence]" in body and 'model = "gpt-5.5"' in body
        assert cs.codex_fast_enabled() is False
        assert cs.set_codex_config_fast(True)
        body = open(tf.name).read()
        assert body.count("service_tier") == 1
        assert body.index("service_tier") < body.index("[model_providers")  # top-level
        assert cs.codex_fast_enabled() is True
        print("  config /fast toggle: off removes, on re-adds top-level, file preserved  OK")
    finally:
        cs._CONFIG = orig
        os.unlink(tf.name)


def test_codex_errors_surface():
    """A failed codex turn (provider timeout / 401 / stream drop) must reach the
    client as an Error, not silence. Transient retries (willRetry) stay quiet."""
    from cc_remote.protocol import Error
    tr = CodexStreamTranslator(8000)
    # transient retry -> nothing
    assert tr.feed({"method": "error", "params": {"willRetry": True, "error": {"message": "Reconnecting... 2/5"}}}) == []
    # terminal error -> Error
    evs = tr.feed({"method": "error", "params": {"willRetry": False,
        "error": {"message": "unexpected status 401 Unauthorized", "additionalDetails": "Incorrect API key"}}})
    assert len(evs) == 1 and isinstance(evs[0], Error) and "401" in evs[0].message and "codex" in evs[0].message
    # failed turn/completed -> surfaces turn.error, then a TurnEnd(is_error)
    tr2 = CodexStreamTranslator(8000)
    out = tr2.feed({"method": "turn/completed", "params": {"turn": {"status": "failed", "error": {"message": "request timed out"}}}})
    assert any(isinstance(e, Error) and "request timed out" in e.message for e in out), out
    assert out[-1].result.is_error is True and out[-1].result.subtype == "error"
    print("  codex errors surface: retry silent, 401 + failed-turn -> Error  OK")


if __name__ == "__main__":
    import os
    test_context_window_capture_and_usage()
    test_context_uses_last_not_cumulative_total()
    test_interrupt_status_maps_to_cc_vocab()
    test_config_fast_toggle_preserves_file()
    test_codex_errors_surface()
    print("ALL PASS")
