"""A real batch poll must never publish a transient unsegmented waterfall."""
from types import SimpleNamespace

import pytest

from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_progress import HookProgressTimeline, emit_progress, register_progress


@pytest.mark.parametrize('segments', [2, 3])
@pytest.mark.parametrize('finished', [False, True])
def test_every_snapshot_of_repeated_batch_poll_preserves_segment_rows(segments, finished):
    timeline = HookProgressTimeline()
    snapshots = []

    def receive(event):
        timeline.apply(event)
        snapshots.append(timeline.model())

    runner = SimpleNamespace(on_runtime_process=receive)
    runtime = HookFormalRuntime.__new__(HookFormalRuntime)
    runtime.lifecycle_runner = runner
    binding = {'binding_id': 'synthetic-binding', 'remark_code': 'TEST01'}
    for segment in range(1, segments + 1):
        data = {'task': {'id': f'task-{segment}'},
                'reply_action': {'segment_count': segments, 'segment_index': segment}}
        runtime.register_batch_progress(binding, 'batch', data)
        for phase in ('send_conditions', 'send_delay', 'pre_send', 'send_submit', 'send_confirm'):
            emit_progress(runner, f'task-{segment}', phase,
                          'current' if not finished and segment == segments and phase == 'send_confirm' else 'done')
    if finished:
        emit_progress(runner, 'batch', 'finished', 'done', 'sent')
    expected = timeline.model()
    snapshots.clear()
    for _ in range(10):
        runtime.register_batch_progress(binding, 'batch', data)
    # Checking only the final snapshot missed the single-frame flash in 0.10.7.
    assert snapshots and all(snapshot == expected for snapshot in snapshots)
    for segment in range(1, segments + 1):
        assert runner._hook_progress_contexts[f'task-{segment}']['segment'] == segment
    assert sum(bool(row['focus']) for row in timeline.model()) == (0 if finished else 1)


def test_partial_alias_refresh_preserves_each_tasks_own_segment_and_new_alias_has_none():
    timeline = HookProgressTimeline()
    runner = SimpleNamespace(on_runtime_process=timeline.apply)
    register_progress(runner, 'one', group='batch', kind='batch', segment=1)
    register_progress(runner, 'two', group='batch', kind='batch', segment=2)
    register_progress(runner, 'batch', kind='batch', aliases=('one', 'two', 'new'))
    for registry in (runner._hook_progress_contexts, timeline.groups):
        assert registry['one']['segment'] == 1
        assert registry['two']['segment'] == 2
        assert registry['new'].get('segment') is None
        assert registry['batch'].get('segment') is None
    # An explicitly supplied correction still takes effect in both registries.
    register_progress(runner, 'two', group='batch', kind='batch', segment=3)
    assert runner._hook_progress_contexts['two']['segment'] == timeline.groups['two']['segment'] == 3


@pytest.mark.parametrize('group,kind', [('other', 'batch'), ('batch', 'customer')])
def test_ui_metadata_does_not_inherit_segment_for_different_round_or_kind(group, kind):
    timeline = HookProgressTimeline()
    timeline.apply(dict(event='hook_progress_group', context='task', aliases=['task'],
                        group='batch', kind='batch', segment=2))
    timeline.apply(dict(event='hook_progress_group', context='task', aliases=['task'],
                        group=group, kind=kind))
    assert timeline.groups['task'].get('segment') is None
