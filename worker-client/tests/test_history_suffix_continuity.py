"""History-only reads use the shared comparator; action gates do not opt in."""
import pytest
from chejin_worker_client.message_viewport_projection import compare_business_viewport_continuity
from test_business_viewport_continuity import fact, sequence


def compare(old, new, *, tokens=True, allow=True):
    return compare_business_viewport_continuity(
        sequence(*(fact(x) for x in old)), sequence(*(fact(x) for x in new)),
        old_boundary_tokens={i: {x} for i, x in enumerate(old)} if tokens else {},
        new_boundary_tokens={i: {x} for i, x in enumerate(new)} if tokens else {},
        allow_history_suffix=allow,
    )


@pytest.mark.parametrize('length,visible', [(11,5), (3,1), (30,20)])
def test_visible_unique_history_suffix_has_no_new_identity(length, visible):
    old = [str(i) for i in range(length)]
    result = compare(old, old[-visible:])
    assert result['relation'] == 'unique_history_suffix_without_new_messages'
    assert result['new_suffix_indexes'] == []
    assert result['matched_pairs'] == [
        {'old_index': length-visible+i, 'new_index': i} for i in range(visible)
    ]


@pytest.mark.parametrize('old,new,tokens', [
    (['a','b','c'], ['b','changed'], True),
    (['a','b','c'], ['a','b'], True),  # Does not reach known history tail.
    (['a','b','c','d'], ['c','b','d'], True),
    (['a','b','c'], [], True),
    (['a','b','c'], ['b','c'], False),
    (['a','b','a','b'], ['a','b'], True),
    (['image','image','image'], ['image','image'], False),
])
def test_unproved_or_changed_suffix_remains_blocked(old, new, tokens):
    assert compare(old,new,tokens=tokens)['relation'] == 'continuity_context_expansion_required'


def test_new_tail_is_still_the_only_new_work():
    result = compare(['a','b','c'], ['b','c','new'])
    assert result['relation'] == 'unique_viewport_slide_with_tail_append'
    assert result['new_suffix_indexes'] == [2]


def test_action_and_pre_send_default_policy_remains_strict():
    assert compare(['a','b','c'], ['b','c'], allow=False)['relation'] == 'continuity_context_expansion_required'


def test_shared_contract_advertises_scoped_history_relation():
    from chejin_worker_client.c2_contract import c2_contract_v3
    from chejin_worker_client.message_viewport_projection import BUSINESS_VIEWPORT_CONTINUITY_RESULTS
    assert 'unique_history_suffix_without_new_messages' in BUSINESS_VIEWPORT_CONTINUITY_RESULTS
    assert c2_contract_v3()['message_identity_lifecycle_contract']['initial_history_suffix_rule']['result'] == 'unique_history_suffix_without_new_messages'


def test_only_initial_checkpoint_alignment_opts_in():
    import ast
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]/'chejin_worker_client'
    owners=[]
    for path in root.glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                if any(isinstance(child,ast.Call) and any(k.arg=='allow_history_suffix' and isinstance(k.value,ast.Constant) and k.value.value is True for k in child.keywords) for child in ast.walk(node)):
                    owners.append((path.name,node.name))
    assert owners == [('task_runner.py','_align_initial_identity_frame')]
