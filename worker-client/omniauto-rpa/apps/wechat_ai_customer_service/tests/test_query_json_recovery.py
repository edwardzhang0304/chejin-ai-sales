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
