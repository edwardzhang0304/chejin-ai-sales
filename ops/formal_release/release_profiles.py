"""Source/evidence profile; publishing and customer rollout require separate authorization."""
import os

SOURCE_BRANCHES = {'ocr':'codex/gray-release-0.9.x', 'hook':'gray-release-0.10.x'}
PRIVATE_PACKAGE_CASES = {'backend/tests/test_hook_send_settlement_entry.py'}


def selected_transport():
    value=os.environ.get('CHEJIN_RELEASE_TRANSPORT','ocr')
    if value not in SOURCE_BRANCHES:
        raise ValueError('RELEASE_TRANSPORT_INVALID')
    return value


def source_branch():
    return SOURCE_BRANCHES[selected_transport()]


def validate_native_source_plan(plan, root):
    if plan.get('reviewed_handoff'):
        raise ValueError('HOOK_REQUIRES_FRESH_SOURCE_TESTS')
    nodes={node for group in plan.get('pytest',[]) for node in group.get('nodes',[])}
    # A Hook build cannot reuse a tiny OCR-only selection. Require its complete
    # native suite plus the original physical-action and updater regressions.
    mandatory={path.relative_to(root).as_posix() for base in ('backend/tests','worker-client/tests')
               for path in (root/base).glob('test_hook*.py')}
    # This case consumes a private, locally generated recovery ZIP. It is not
    # reproducible in public source CI; keep it for separate package acceptance.
    mandatory.difference_update(PRIVATE_PACKAGE_CASES)
    mandatory.update({
        'worker-client/tests/test_task_runner.py',
        'worker-client/tests/test_client_update.py',
        'worker-client/tests/test_update_coordinator.py',
        'worker-client/tests/test_chejin_updater.py',
        'backend/tests/test_ai_capacity.py',
    })
    if not mandatory or not mandatory.issubset(nodes):
        raise ValueError('HOOK_SOURCE_TEST_PLAN_INCOMPLETE')
