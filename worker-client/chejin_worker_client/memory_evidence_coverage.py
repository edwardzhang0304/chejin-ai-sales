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
            {'area': 'python_allocations', 'mode': 'off_unless_explicit_startup_flag',
             'evidence': 'logs/runtime/python-memory/*.json; logs/runtime/python-memory-status.json; logs/runtime/python-memory-gap.json',
             'coverage': 'bounded_sampled_live_allocations_by_stack',
             'gap': 'Only an explicit startup window; cannot recover allocations from later days. No objects, owners/reference graphs, pre-start allocations or untracked native memory; cooperative budgets.'},
            {'area': 'caches_queues_and_lifecycle', 'mode': 'selected_existing_gauges',
             'evidence': 'performance', 'coverage': 'history_cache_and_selected_inflight_inventory',
             'gap': 'Not complete ownership accounting for every response, audio, image, UI object, thread, pipe or queue.'},
            {'area': 'qt_win32_wechat_dll_native', 'mode': 'not_instrumented',
             'evidence': 'process counters only', 'coverage': 'none_for_allocation_ownership',
             'gap': 'Requires separate authorized native profiler/Windows experiment; Python reports cannot attribute this memory.'},
        ],
        'restart': 'Persisted files survive; an unfinished profile is not proof of successful completion.',
        'business_actions': 'Export neither calls DLL diagnostics nor starts tracing, trims, retries or clears records.',
    }
