import json
from pathlib import Path
import pytest
from test_brain_completion_integrity import provider, brain
from test_query_json_recovery import sized_query, inputs, output

@pytest.mark.parametrize('network_first', [False, True])
def test_oversize_recovery_keeps_skipped_input_evidence(provider, network_first):
    outputs,calls=provider
    q,raw=sized_query(4154)
    prefix=[{'ok':False,'status':503,'error':'temporary upstream failure'}] if network_first else []
    outputs.extend(prefix+[output(raw),output(json.dumps({'catalog_query':q}))])
    result=brain.run_brain_llm(settings={'provider':'openai'},brain_input=inputs())
    assert result['ok'] and result['brain_plan']=={'catalog_query':q}
    assert not any(c['progress_stage'].endswith('json_structure_repair') for c in calls)
    audits=[]
    def walk(v):
        if isinstance(v,dict):
            if 'repair_input' in v:audits.append(v['repair_input'])
            for x in v.values():walk(x)
        elif isinstance(v,list):
            for x in v:walk(x)
    walk(result)
    assert any(a['original_chars']==4154 and a['submitted_chars']==0 for a in audits), 'Oversize skip evidence lost after successful recovery'
    if network_first:
        intermediate = result['previous_llm_status']
        assert intermediate['generation_stage'] == 'unavailable_retry' and intermediate['status'] == 200
        initial = intermediate['previous_llm_status']
        assert initial['generation_stage'] == 'primary' and initial['status'] == 503
        assert initial['error'] == 'temporary upstream failure'
