"""Synthetic malformed responses through existing generation recovery boundaries."""
import json

import pytest

from test_brain_completion_integrity import provider, brain
from test_catalog_query import query, cq, evidence
from llm_output_adapter import llm_json_parse_evidence, parse_complete_llm_json_response, parse_llm_json_object


def output(text):
    return {"ok": True, "status": 200, "response_text": text,
            "response_diagnostics": {"finish_reason": "stop"}}


def malformed():
    return json.dumps({"catalog_query": query()}, ensure_ascii=False).replace('"catalog_query":', '"catalog_query"', 1)


def inputs(phase="not_queried"):
    return {"evidence": {"catalog_protocol": {"phase": phase}}}


@pytest.mark.parametrize("prefix", [[], [output("")],
    [{"ok": False, "status": 503, "error": "temporary upstream failure"}]])
def test_query_contract_survives_all_existing_recovery_entries(provider, prefix):
    outputs, calls = provider
    outputs.extend(prefix + [output(malformed()), output(json.dumps({"catalog_query": query()}))])
    result = brain.run_brain_llm(settings={"provider": "openai"}, brain_input=inputs())
    assert result["ok"] and result["json_structure_repaired"]
    repair = next(c for c in calls if c["progress_stage"].endswith("json_structure_repair"))
    request = json.loads(repair["messages"][-1]["content"])
    assert cq.QUERY_FORMAT_INSTRUCTION in request["schema"]
    assert request["raw_brain_output"] == malformed()
    assert "不得新增、删除、改写查询条件" in repair["messages"][0]["content"]
    q = result["brain_plan"]["catalog_query"]
    assert cq.normalize_query(q, cq.message_sources(evidence())) == cq.normalize_query(query(), cq.message_sources(evidence()))
    assert result["json_structure_repair"]["parse_evidence"]["kind"] == "invalid_json"
    assert len(calls) == len(prefix) + 2


def test_post_query_reply_repair_does_not_offer_a_second_query(provider):
    outputs, calls = provider
    outputs.extend([output('{"can_answer" true}'), output('{"can_answer":true}')])
    result = brain.run_brain_llm(settings={"provider": "openai"}, brain_input=inputs("queried"))
    assert result["ok"]
    request = json.loads(calls[1]["messages"][-1]["content"])
    assert cq.QUERY_FORMAT_INSTRUCTION not in request["schema"]
    assert request["task"] == "repair_malformed_brain_plan_json"


def test_repeated_invalid_json_has_no_plan_and_bounded_retries(provider):
    outputs, calls = provider
    outputs.extend([output(malformed())] * 4)
    result = brain.run_brain_llm(settings={"provider": "openai"}, brain_input=inputs())
    assert not result["ok"] and not result.get("brain_plan")
    assert len(calls) == 4  # Existing initial/structure-repair/same-capture/structure-repair policy.
    assert all(cq.QUERY_FORMAT_INSTRUCTION in json.loads(c["messages"][-1]["content"])["schema"]
               for c in calls if c["progress_stage"].endswith("json_structure_repair"))
    details = result["json_structure_repair"]
    assert details["parse_evidence"]["kind"] == details["repair_parse_evidence"]["kind"] == "invalid_json"


@pytest.mark.parametrize("raw", ['{"a":1,"b" "private-value"}', '{"a":1,}', '{"a":{"b":2},"c" "private"}'])
def test_complete_parser_never_drops_invalid_fields_to_salvage_json(raw):
    assert isinstance(parse_llm_json_object(raw), dict)  # Compatibility-only legacy caller.
    assert parse_complete_llm_json_response(output(raw)) is None


def test_syntax_evidence_is_bounded_and_has_no_customer_values():
    raw = '{"private-name":"13812345678","private-field" "private-value"}'
    details = llm_json_parse_evidence(output(raw))
    assert details["kind"] == "invalid_json" and details["position"] == raw.index('"private-value"')
    assert len(details["syntax_window"]) <= 81 and len(details["response_sha256"]) == 64
    assert "private" not in json.dumps(details) and "13812345678" not in json.dumps(details)
    assert llm_json_parse_evidence(output('[]'))["kind"] == "non_object"


@pytest.mark.parametrize("raw", [
    '{"verdict":"pass"}\n{"verdict":"block"}',
    '{"verdict":"block"}\n{"verdict":"pass"}',
    '[{"verdict":"pass"},{"verdict":"block"}]',
    '[{"verdict":"pass"}]',
    '{"verdict":"pass","verdict":"block"}',
    r'{"verdict":"pass","ver\u0064ict":"block"}',
    '{"nested":{"source":"first","source":"second"}}',
    '{"verdict":"pass"},"verdict":"block"',
    '{"verdict":"pass"} null',
    '```json\n{"verdict":"pass"}\n```\n```json\n{"verdict":"block"}\n```',
])
def test_complete_entry_rejects_ambiguous_structure(raw):
    assert parse_complete_llm_json_response(output(raw)) is None
    assert llm_json_parse_evidence(output(raw))["kind"] in {"ambiguous_structure", "non_object"}


def test_ambiguous_generation_regenerates_from_context_without_picking_an_object(provider):
    outputs, calls = provider
    raw = json.dumps({"catalog_query": query()}) + '{"can_answer":false}'
    outputs.extend([output(raw), output(json.dumps({"catalog_query": query()}))])
    result = brain.run_brain_llm(settings={"provider": "openai"}, brain_input=inputs())
    assert result["ok"] and result["brain_plan"] == {"catalog_query": query()}
    assert len(calls) == 2 and all(not c["progress_stage"].endswith("json_structure_repair") for c in calls)
    assert calls[0]["messages"] == calls[1]["messages"]
    assert result["previous_llm_status"]["json_structure_repair"]["parse_evidence"]["kind"] == "ambiguous_structure"


def sized_query(size):
    # All critical business conditions are placed after the long text/name fields.
    q = query(interpretation="需求", name_queries=[{"text": str(i), "kind": "exclude", "source_ids": ["m1"]} for i in range(10)],
              exclude_ids=["known-excluded"], unsupported_requirements=["需人工核实年份"])
    tail = {key: q.pop(key) for key in ["budget", "exclude_ids", "source_ids", "unsupported_requirements"]}
    q.update(tail)
    def raw():
        return json.dumps({"catalog_query": q}, ensure_ascii=False).replace('"catalog_query":', '"catalog_query"', 1)
    for item, key, limit in [(q, "interpretation", 1500), *[(n, "text", 200) for n in q["name_queries"]]]:
        extra = min(size - len(raw()), limit - len(item[key]))
        if extra > 0: item[key] += "合" * extra
    assert len(raw()) == size
    cq.normalize_query(q, cq.message_sources(evidence()), known_ids={"known-excluded": [{"source_id": "m1"}]})
    return q, raw()


@pytest.mark.parametrize("size", [2999, 3000, 3001])
@pytest.mark.parametrize("phase", ["not_queried", "queried"])
def test_structure_repair_complete_or_skipped_at_exact_limit(provider, size, phase):
    outputs, calls = provider
    q, raw = sized_query(size)
    if size <= 3000: outputs.append(output(json.dumps({"catalog_query": q})))
    result = brain.maybe_repair_brain_json_structure(settings={"provider": "openai"}, brain_input=inputs(phase), raw_text=raw, stage="test")
    audit = result["repair_input"]
    assert audit["original_chars"] == size and audit["limit_chars"] == 3000 and not audit["truncated"]
    if size <= 3000:
        assert result["ok"] and len(calls) == 1
        assert json.loads(calls[0]["messages"][-1]["content"])["raw_brain_output"] == raw
        assert audit["submitted_chars"] == size and audit["action"] == "submitted_complete"
    else:
        assert not result["ok"] and not result["attempted"] and calls == []
        assert audit["submitted_chars"] == 0 and result["error"] == "brain_structure_repair_input_too_large"


@pytest.mark.parametrize("prefix", [[], [output("")],
    [{"ok": False, "status": 503, "error": "temporary upstream failure"}]])
def test_oversize_query_uses_existing_context_recovery_or_fails_closed(provider, prefix):
    outputs, calls = provider
    q, raw = sized_query(4154)
    complete = output(json.dumps({"catalog_query": q}))
    # After an existing parse retry there is no extra retry allowance.
    in_parse_retry = bool(prefix and prefix[0].get("ok"))
    outputs.extend(prefix + [output(raw)] + ([] if in_parse_retry else [complete]))
    result = brain.run_brain_llm(settings={"provider": "openai"}, brain_input=inputs())
    assert len(calls) == (2 if in_parse_retry else len(prefix) + 2)
    assert all(not c["progress_stage"].endswith("json_structure_repair") for c in calls)
    assert all(c["messages"] == calls[0]["messages"] for c in calls)
    if in_parse_retry:
        assert not result["ok"] and not result.get("brain_plan")
        audit = result["same_capture_retry"]["json_structure_repair"]["repair_input"]
        assert audit["original_chars"] == 4154 and audit["submitted_chars"] == 0
    else:
        assert result["ok"] and result["brain_plan"] == {"catalog_query": q}


def test_reviewer_text_override_has_the_same_unambiguous_boundary():
    import sys
    reviewer = sys.modules[brain.review_brain_reply_semantics.__module__]
    raw = '{"verdict":"pass"}{"verdict":"block"}'
    result = reviewer.reviewer_from_settings({"semantic_reviewer_response_text": raw})
    assert result["status"] == "error" and result["error_kind"] == "protocol"
    assert result["parse_evidence"]["kind"] == "ambiguous_structure"
