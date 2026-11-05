"""Read-only capability map exported even when optional probes never ran."""
def coverage():
    return {
        'schema': 1, 'scope': 'capabilities_not_proof_of_collection',
        'availability': 'See ZIP collection manifest for missing, failed or omitted files; missing is never zero.',
        'areas': [
            {'area': 'process_resources', 'mode': 'normal_hook_runtime',
             'evidence': 'performance and logs/runtime', 'coverage': 'process_private_rss_handles_threads_io',
             'gap': 'No native allocation owners; unavailable OS counters remain null.'},
            {'area': 'worker_to_dll_calls', 'mode': 'normal_gateway',
             'evidence': 'state/hook_transport_events.json', 'coverage': 'before_after_process_net_change',
             'gap': 'Not DLL allocation bytes; concurrent work and delayed frees confound deltas.'},
            {'area': 'dll_stats_and_trim', 'mode': 'explicit_engineering_probe_only',
             'evidence': 'state/hook_transport_events.json', 'coverage': 'bounded_redacted_provider_summary',
             'gap': 'Provider fields, units and coverage unverified; trim is not leak repair.'},
            {'area': 'python_allocations', 'mode': 'running_worker_requested_by_desktop_diagnostics',
             'evidence': 'logs/runtime/python-memory/*.json; logs/runtime/python-memory-status.json; logs/runtime/python-memory-gap.json',
             'coverage': 'bounded_sampled_live_allocations_by_stack',
             'gap': 'Only new allocations after the current bounded request; cannot reconstruct prior days. No objects, owners/reference graphs, pre-start allocations or untracked native memory; cooperative budgets.'},
            {'area': 'caches_queues_and_lifecycle', 'mode': 'selected_existing_gauges',
             'evidence': 'performance', 'coverage': 'history_cache_and_selected_inflight_inventory',
             'gap': 'Not complete ownership accounting for every response, audio, image, UI object, thread, pipe or queue.'},
            {'area': 'qt_win32_wechat_dll_native', 'mode': 'not_instrumented',
             'evidence': 'process counters only', 'coverage': 'none_for_allocation_ownership',
             'gap': 'Requires separate authorized native profiler/Windows experiment; Python reports cannot attribute this memory.'},
        ],
        'restart': 'Persisted files survive; an unfinished profile is not proof of successful completion.',
        'business_actions': 'Desktop ExportJob requests bounded tracing in the original Worker; Collector is read-only. Neither calls DLL diagnostics, trims, retries or clears business records.',
    }


def current_collection(request, entries):
    """Actual collected sources, separate from the capability description."""
    def source(prefixes):
        matches = [e for e in entries if str(e.get('source', '')).startswith(prefixes)
                   or str(e.get('archive_path', '')).startswith(prefixes)]
        collected = [e for e in matches if e.get('status') == 'collected']
        return {'status': 'source_files_exported' if collected else 'missing',
                'sources': [e.get('archive_path', e.get('source')) for e in collected[:32]],
                'issues': [e for e in matches if e.get('status') != 'collected'][:16],
                'scope': 'source_container_availability_only_verify_original_process_samples_and_call_rows'}
    return {'schema': 1, 'request_id': (request or {}).get('request_id'),
        'worker_python': {'status': (request or {}).get('status', 'not_requested'),
                          'capture_id': (request or {}).get('capture_id'),
                          'reason': (request or {}).get('reason'),
                          'new_capture': (request or {}).get('new_capture', False)},
        'wechat_resources': source(('performance/',)),
        'dll_calls': source(('state/hook_transport_events',)),
        'DiySceneStats': {'status': 'deferred_not_implemented_for_running_capture',
                         'reason': 'deferred_by_user_current_release',
                         'executed_this_request': False},
        'SwapMemory': {'status': 'not_executed',
                       'reason': 'mutates_working_set_not_read_only_evidence',
                       'executed_this_request': False}}
