"""Rejected model fields must cross the actual catalog boundary as validation errors."""
from copy import deepcopy
import pytest
from test_catalog_query import cq, query, evidence


@pytest.mark.parametrize("path", ["operation", "requirement_state", "sort", "name_queries.0.kind", "filters.0.field", "filters.0.op", "budget.mode"])
@pytest.mark.parametrize("bad", [[], {}, None, True, 1])
def test_enum_types_are_classified_with_exact_field_evidence(path, bad):
    q = query(name_queries=[{"text": "合成", "kind": "exclude", "source_ids": ["m1"]}],
              filters=[{"field": "brand", "op": "eq", "value": "合成", "source_ids": ["m1"]}])
    target = q
    parts = path.split(".")
    for key in parts[:-1]: target = target[int(key)] if isinstance(target, list) else target[key]
    target[parts[-1]] = deepcopy(bad)
    result = cq.run_catalog_round(first_result={"ok": True, "status": 200, "brain_plan": {"catalog_query": q}},
        settings={}, brain_input={"evidence": evidence()}, runtime_factory=lambda: pytest.fail("invalid query read inventory"),
        run_llm=lambda **_: pytest.fail("invalid query reached completion"))
    assert result["error_kind"] == "catalog_query_validation"
    error = result["catalog_stages"]["validation_error"]
    assert error["fields"] == [path]
    assert error["field_evidence"][0]["present"] and error["field_evidence"][0]["value_type"] == type(bad).__name__
    assert result["http_status"] == 200


@pytest.mark.parametrize("value", [[], {}, None, True, "4", float('inf'), float('nan'), 10**400])
def test_bad_numeric_budget_has_a_controlled_field_error(value):
    q = query(); q["budget"]["value"] = value
    with pytest.raises(cq.CatalogQueryError) as caught: cq.normalize_query(q, cq.message_sources(evidence()))
    assert caught.value.fields == ["budget.value"]


@pytest.mark.parametrize("where", ["validation_internal", "inventory"])
def test_true_internal_type_error_is_not_relabelled_as_customer_field(monkeypatch, where):
    def internal(*args, **kwargs): raise TypeError("synthetic internal defect")
    if where == "validation_internal": monkeypatch.setattr(cq, "normalize_query", internal)
    result = cq.run_catalog_round(first_result={"ok": True, "status": 200, "brain_plan": {"catalog_query": query()}},
        settings={}, brain_input={"evidence": evidence()}, runtime_factory=internal,
        run_llm=lambda **_: pytest.fail("internal error reached completion"))
    assert result["error_kind"] == "catalog_query_execution"
    assert "validation_error" not in result["catalog_stages"]
