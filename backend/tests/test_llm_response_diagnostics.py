"""Artificial HTTP responses verify metadata capture; no live model is called."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

import test_brain_fact_guidance_regression as regression
from app.services.ai_adapter import _read_provider_progress, _safe_response_diagnostics
from apps.wechat_ai_customer_service import llm_config

SECRET = "PRIVATE-KEY-SENTINEL"
TEXT = "PRIVATE-REPLY-SENTINEL"
THINKING = "PRIVATE-THINKING-SENTINEL"


@pytest.fixture
def http_provider(monkeypatch, tmp_path):
    state = {"body": {}, "status": 200, "calls": 0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            state["calls"] += 1
            body = state["body"] if isinstance(state["body"], bytes) else json.dumps(state["body"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("x-request-id", "test-request-id")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    progress = tmp_path / "progress.jsonl"
    monkeypatch.setenv("CHEJIN_AI_PROGRESS_PATH", str(progress))
    monkeypatch.setenv("CHEJIN_AI_PROGRESS_ID", "diag-test")
    def call(body, *, provider="openai_compatible", status=200):
        state.update(body=body, status=status)
        return llm_config.call_llm_request_with_failover(
            provider=provider, api_key=SECRET, base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model="test-model", messages=[{"role": "user", "content": "PRIVATE-CUSTOMER-SENTINEL"}],
            timeout=3, max_tokens=8192, allow_fallback=False, progress_stage="brain_llm",
        )
    yield call, progress, state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _response(content, *, finish="stop", **message_fields):
    return {"choices": [{"message": {"content": content, **message_fields}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 23, "completion_tokens_details": {"reasoning_tokens": 23}}}


@pytest.mark.parametrize("content,finish,kind,chars", [
    (TEXT, "stop", "string", len(TEXT)), ("", "stop", "string", 0),
    (None, "stop", "null", 0), (None, "length", "null", 0),
    ("   ", "stop", "string", 0),
    ([{"type": "text", "text": TEXT}], "stop", "list", len(TEXT)),
])
def test_wire_content_and_extraction_are_recorded_separately(http_provider, content, finish, kind, chars):
    call, progress, state = http_provider
    result = call(_response(content, finish=finish, reasoning_content=THINKING))
    assert result["ok"] is True
    diag = result["response_diagnostics"]
    assert diag["content_type"] == kind
    assert diag["content_nonblank_chars"] == chars
    assert diag["extracted_chars"] == len(result["response_text"])
    assert diag["reasoning_chars"] == len(THINKING)
    assert diag["finish_reason"] == finish
    assert diag["requested_max_tokens"] == 8192
    assert diag["reasoning_tokens"] == 23
    assert diag["body_bytes"] > 0
    events = _read_provider_progress(progress, progress_id="diag-test")
    assert events[-1]["response_diagnostics"] == diag
    assert events[-1]["provider_request_id"] == "test-request-id"
    raw = progress.read_text()
    for private in (SECRET, TEXT, THINKING, "PRIVATE-CUSTOMER-SENTINEL"):
        assert private not in raw
    assert state["calls"] == 1


def test_nonempty_wire_content_with_empty_extractor_is_detectable(monkeypatch, http_provider):
    call, _, _ = http_provider
    # Intentional extractor fault injection; never a change to production behavior.
    monkeypatch.setattr(llm_config, "extract_llm_response_text", lambda **_kwargs: "")
    diag = call(_response(TEXT))["response_diagnostics"]
    assert diag["content_nonblank_chars"] > 0
    assert diag["extracted_chars"] == 0


@pytest.mark.parametrize("body,expected", [
    (b"not-json", {"json_decode_attempted": True, "json_decoded": False, "extraction_attempted": False}),
    ({"choices": []}, {"json_decoded": True, "choices_count": 0, "extraction_attempted": True, "extraction_succeeded": False}),
    ({"choices": [{"message": {}}]}, {"content_type": "missing", "extracted_chars": 0}),
    (_response(None, refusal=TEXT, finish="content_filter"), {"refusal_chars": len(TEXT), "finish_reason": "content_filter"}),
    (_response(None, tool_calls=[{"id": TEXT}], finish="tool_calls"), {"tool_calls_count": 1, "finish_reason": "tool_calls"}),
    (_response(None, finish=SECRET), {"finish_reason": "other"}),
])
def test_failure_and_alternative_response_shapes_have_bounded_evidence(http_provider, body, expected):
    call, progress, _ = http_provider
    result = call(body)
    diag = result["response_diagnostics"]
    assert diag["http_status"] == 200
    for key, value in expected.items():
        assert diag[key] == value
    assert _read_provider_progress(progress, progress_id="diag-test")[-1]["response_diagnostics"] == diag
    assert SECRET not in progress.read_text()
    assert TEXT not in progress.read_text()


def test_anthropic_text_blocks_are_counted_without_saving_thinking(http_provider):
    call, progress, _ = http_provider
    result = call({"content": [{"type": "thinking", "thinking": THINKING}, {"type": "text", "text": TEXT}], "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 8}}, provider="anthropic")
    assert result["response_text"] == TEXT
    diag = result["response_diagnostics"]
    assert diag["content_nonblank_chars"] == len(TEXT)
    assert diag["reasoning_chars"] == len(THINKING)
    assert diag["completion_tokens"] == 8
    assert diag["reasoning_tokens"] is None
    assert THINKING not in progress.read_text()


def test_http_error_is_not_misreported_as_successful_empty_content(http_provider):
    call, progress, _ = http_provider
    result = call({"error": TEXT}, status=502)
    assert result["ok"] is False and result["status"] == 502
    diag = _read_provider_progress(progress, progress_id="diag-test")[-1]["response_diagnostics"]
    assert diag["http_status"] == 502
    assert diag["body_read_complete"] is True
    assert diag["json_decode_attempted"] is False
    assert TEXT not in progress.read_text()


@pytest.mark.parametrize("failed_component", ["collector", "writer"])
def test_diagnostics_failure_cannot_change_model_result(monkeypatch, http_provider, failed_component):
    call, _, state = http_provider
    def fail(*_args, **_kwargs):
        raise OSError("artificial diagnostic failure")
    monkeypatch.setattr(llm_config, "_llm_response_structure" if failed_component == "collector" else "_emit_llm_progress_event", fail)
    result = call(_response(TEXT))
    assert result["ok"] is True and result["response_text"] == TEXT
    assert state["calls"] == 1


def test_backend_drops_injected_text_and_invalid_diagnostic_types():
    result = _safe_response_diagnostics({"schema_version": 1, "content": TEXT, "api_key": SECRET,
        "body_bytes": SECRET, "content_type": [SECRET], "finish_reason": SECRET,
        "reasoning_tokens": -1, "requested_max_tokens": 10**20, "content_chars": True,
        "extraction_succeeded": "true", "extracted_chars": 0})
    assert result == {"schema_version": 1, "extracted_chars": 0}


@pytest.mark.parametrize("field,count,empty,present,size", [
    ("reasoning_content", "reasoning_chars", "", THINKING, len(THINKING)),
    ("refusal", "refusal_chars", "", TEXT, len(TEXT)),
    ("tool_calls", "tool_calls_count", [], [{"id": "synthetic-tool"}], 1),
])
@pytest.mark.parametrize("state", ["missing", "null", "wrong_type", "empty", "present"])
def test_optional_counts_preserve_unknown_through_http_and_progress(
    http_provider, field, count, empty, present, size, state,
):
    call, progress, calls = http_provider
    body = _response(TEXT)
    if state != "missing":
        body["choices"][0]["message"][field] = {
            "null": None, "wrong_type": {}, "empty": empty, "present": present,
        }[state]
    result = call(body)
    expected = 0 if state == "empty" else size if state == "present" else None
    assert result["response_diagnostics"][count] == expected
    assert _read_provider_progress(progress, progress_id="diag-test")[-1]["response_diagnostics"][count] == expected
    assert result["ok"] and result["response_text"] == TEXT
    assert calls["calls"] == 1
    assert all(private not in progress.read_text() for private in (SECRET, TEXT, THINKING))


@pytest.mark.parametrize("content,reasoning_count,tools_count", [
    (None, None, None),
    ({}, None, None),
    ([{"type": "text", "text": TEXT}], None, None),
    ([{"type": "thinking"}], None, None),
    ([{"type": "thinking", "thinking": None}], None, None),
    ([{"type": "thinking", "thinking": ""}], 0, None),
    ([{"type": "thinking", "thinking": THINKING}, {"type": "tool_use", "id": "synthetic-tool"}], len(THINKING), 1),
    ([{"type": "redacted_thinking", "data": "synthetic-redacted"}], None, None),
    ([{"type": "thinking", "thinking": THINKING}, {"type": "redacted_thinking", "data": "synthetic-redacted"}], None, None),
    ([{"type": {}, "thinking": THINKING}], None, None),
])
def test_anthropic_optional_counts_only_measure_readable_reported_blocks(content, reasoning_count, tools_count):
    diag = llm_config._llm_response_structure({"content": content}, "anthropic_messages")
    assert diag["reasoning_chars"] == reasoning_count
    assert diag["tool_calls_count"] == tools_count
    assert diag["refusal_chars"] is None


def test_empty_response_and_retry_evidence_survive_real_brain_and_database(monkeypatch):
    regression.test_c3_api_database_real_brain_and_provider_preserve_guided_reply(
        monkeypatch, 0, False, empty_first=True,
    )


def test_quality_repair_keeps_its_own_call_evidence_in_database(monkeypatch):
    from app.models.c3 import MessageBatch
    import test_c3_api as api
    regression.test_c3_api_database_real_brain_and_provider_preserve_guided_reply(monkeypatch, 0, True)
    with api.SessionLocal() as db:
        batch = db.query(MessageBatch).one()
        result = batch.ai_response_snapshot["raw_payload"]["omniauto_brain_result"]
        events = [event for event in result["provider_progress"] if "response_diagnostics" in event]
    assert events[0]["stage"] == "brain_llm"
    assert events[1]["stage"] == "brain_quality_repair"
    assert events[0]["call_id"] != events[1]["call_id"]
    assert all(event["response_diagnostics"]["extracted_chars"] > 0 for event in events)
