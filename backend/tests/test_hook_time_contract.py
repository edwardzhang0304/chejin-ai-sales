"""Public contract derives request shapes and pure-clock fingerprint from code."""
from pathlib import Path
import hashlib
import json
from app.schemas import hook_business


def test_time_contract_matches_producers_and_consumers():
    root=Path(__file__).parents[2]
    contract=json.loads((root/'contracts/hook_contract_v1.json').read_text())
    for name in ('HookInviteSubmission','HookNativeSendClaim','HookNativeSendBegin','HookNativeSendSettlement'):
        assert contract['formal_business']['request_schemas'][name]==getattr(hook_business,name).model_json_schema()
    rules=contract['time_rules']
    assert hashlib.sha256((root/rules['rules_source']).read_bytes()).hexdigest()==rules['rules_source_sha256']
