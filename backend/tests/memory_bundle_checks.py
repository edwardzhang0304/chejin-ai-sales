"""C6: inspect actual exporter output, never inject evidence into the ZIP."""
import hashlib
import json
import zipfile


def check_bundle(path, report, chain, worker_token):
    with zipfile.ZipFile(path) as archive:
        def read(name):
            return json.loads(archive.read(name))
        manifest = read('收集清单.json')
        collected = [s for s in manifest['sources'] if s['status'] == 'collected']
        for source in collected:
            raw = archive.read(source['archive_path'])
            assert len(raw) == source['bytes']
            assert hashlib.sha256(raw).hexdigest() == source['sha256']
        assert not any(worker_token.encode() in archive.read(n) for n in archive.namelist())
        collection = read('memory/collection.json')
        assert collection['DiySceneStats']['executed_this_request'] is False
        assert collection['SwapMemory']['executed_this_request'] is False
        captures = report.get('capture_requests', [])
        if captures:
            request = read('memory/request.json')
            assert request['request_id'] == captures[0]['request_id']
            assert request['target']['pid'] == report['pid']
            assert collection['worker_python']['capture_id'] == request['capture_id']
        for request in captures:
            profile = read('logs/runtime/python-memory/' + request['report_file'])
            assert profile['status'] == request['status'] == 'finished'
            assert profile['request_id'] == request['request_id']
            assert profile['capture_id'] == request['capture_id']
            for key in ('pid', 'created', 'runtime_session'):
                assert profile[key] == request['target'][key]
            assert profile['duration_limit_seconds'] == 30
            assert profile['ended_monotonic'] - profile['started_monotonic'] >= 30
            assert profile['limits']['capture_seconds'] == .5
            assert profile['object_contents_collected'] is False
            assert all(s['total_observer_ms'] <= 500 for s in profile['samples'])
            if any(s['omitted_sites'] for s in profile['samples']):
                assert any(s.get('reason') == 'python_profile_detail_budget'
                           for s in manifest['sources'])
        # Original production writers now use the formal transactions directory.
        # Exported files are redacted, so their hashes are archive hashes; the
        # recovery scenario separately verifies unchanged original-file hashes.
        for action in chain['actions']:
            aid = action['id']
            receipt = read('receipts/hook/send-receipts/' + aid + '.json')
            assert receipt['automatic_resend_allowed'] is False
            assert receipt['ack']['id'] in {a['id'] for a in chain['acks']}
            assert receipt['ack']['task_id'] == action['claimed_task_id']
            # Native journals are scoped by worker / account epoch / action.
            intents = [name for name in archive.namelist()
                       if name.startswith('receipts/hook/sends/')
                       and name.endswith('/' + aid + '/intent.json')]
            assert len(intents) == 1, (aid, intents)
            intent = read(intents[0])
            assert intent['reply_action_id'] == aid
            assert intent['task_id'] == action['claimed_task_id']
        samples = [json.loads(line) for name in archive.namelist()
                   if name.startswith('performance/') and name.endswith('.jsonl')
                   for line in archive.read(name).splitlines()]
        worker = [s['resources']['worker'] for s in samples]
        wechat = [s['resources']['wechat'] for s in samples if 'wechat' in s['resources']]
        assert worker and wechat
        assert all(s['pid'] == report['pid'] and s['identity_valid'] for s in worker)
        assert all(s['pid'] == report['driver_backend_pid'] and s['identity_valid'] for s in wechat)
        for sample in worker + wechat:
            assert sample['rss_bytes'] > 0 and sample['created'] > 0
            for key in ('private_bytes', 'handles', 'io_bytes', 'cpu_percent'):
                if sample[key] is None:
                    assert sample['unavailable'].get(key)
        calls = read('state/hook_transport_events.json')['rows']
        finished = [json.loads(row['evidence']) for row in calls
                    if json.loads(row['evidence']).get('phase') == 'finished']
        assert finished
        for call in finished:
            assert call['duration_ms'] >= 0 and call['request_bytes'] > 0
            assert call['response_bytes'] > 0 and call['response_complete']
            memory = call['memory']
            assert memory['attribution'] == 'process_net_change_not_dll_allocation'
            assert memory['peak_measured'] is memory['delayed_release_measured'] is False
            for phase in ('before', 'after'):
                assert memory[phase]['caller']['pid'] == report['pid']
                assert memory[phase]['wechat']['pid'] == report['driver_backend_pid']
                assert len(memory[phase]['dll_sha256']) == 64
                for role in ('caller', 'wechat'):
                    sample = memory[phase][role]
                    assert sample['identity_valid'] and sample['created'] > 0
                    if sample['private_bytes'] is None:
                        assert sample['unavailable']['private_bytes']
        return {'collected_hashes_checked': len(collected), 'profiles_checked': len(captures),
                'send_proofs_checked': len(chain['actions']), 'resource_samples': len(samples),
                'hook_finished_calls': len(finished), 'manifest_partial': manifest['partial'],
                'physical_boundary': 'real_test_process_counters_not_wechat_or_dll_attribution'}
