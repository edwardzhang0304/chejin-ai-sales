from copy import deepcopy
import pytest
from chejin_worker_client.shared_rules import hook_time_rules as rules

BASE='2026-10-28T12:22:'

def window():
    return {'source':'physical_call_v1','started_at':BASE+'46+00:00',
        'completed_at':BASE+'46.2+00:00','elapsed_seconds':.2}


def test_offset_does_not_change_original_backend_lease_time():
    assert rules.call_window(window(),BASE+'44+00:00',BASE+'44.3+00:00')[1]-rules.call_window(window(),BASE+'44+00:00',BASE+'44.3+00:00')[0]==1

@pytest.mark.parametrize('case',['wall_jump','negative','nan','bool','extra','timezone','too_long'])
def test_invalid_local_clock_never_expands_confirmation_window(case):
    value=deepcopy(window())
    if case=='wall_jump':value['elapsed_seconds']=4
    if case=='negative':value['elapsed_seconds']=-1
    if case=='nan':value['elapsed_seconds']=float('nan')
    if case=='bool':value['elapsed_seconds']=True
    if case=='extra':value['offset']=10
    if case=='timezone':value['started_at']=BASE+'46'
    if case=='too_long':value.update(completed_at=BASE+'48+00:00',elapsed_seconds=2)
    with pytest.raises(ValueError):rules.call_window(value,BASE+'44+00:00',BASE+'44.3+00:00')
