"""Both shipping facades consume the same source; no API/data writes."""
from copy import deepcopy
from pathlib import Path
import sys
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'worker-client'))
from app.contracts import c2 as backend
from app.services import task_service,wechat_service
from chejin_worker_client import c2_contract as worker,task_runner,storage,wechat_c2

def test_pre_send_errors_are_derived_from_current_contract():
    contract=backend.c2_contract_v3()
    expected=set(contract['pre_send_message_viewport_contract']['pre_send_specific_errors'])-{'C2_PRE_SEND_LAYOUT_INVALID','C2_PRE_SEND_FACT_CHECKPOINT_INVALID'}
    assert task_service._PRE_SEND_REIDENTIFICATION_ERRORS==task_runner.PRE_SEND_REIDENTIFICATION_ERRORS==expected
    altered=deepcopy(contract)
    altered['pre_send_message_viewport_contract']['pre_send_specific_errors'].append('SYNTHETIC_NEW_CONTRACT_ENTRY')
    with patch.object(backend,'c2_contract_v3',return_value=altered),patch.object(worker,'c2_contract_v3',return_value=altered):
        assert backend.pre_send_reidentification_errors()==worker.pre_send_reidentification_errors()==expected|{'SYNTHETIC_NEW_CONTRACT_ENTRY'}

def test_image_forbidden_prefixes_share_one_authority():
    expected=('provider_response','raw_provider_response','retry_response','initial_response')
    assert backend.IMAGE_FORBIDDEN_FIELD_PREFIXES==worker.IMAGE_FORBIDDEN_FIELD_PREFIXES==expected
    assert wechat_service.IMAGE_FORBIDDEN_FIELD_PREFIXES==wechat_c2.IMAGE_RUNTIME_FIELD_PREFIXES==storage._OUTBOX_FORBIDDEN_KEY_PREFIXES==expected
