"""Shared pure upgrade report checks for CI and installed operator tools."""
from verify import require


def validate_case(name, report, plan, proof):
    old = plan['old_client']; stem = f"chejin-worker-v{proof['version']}-windows-x64"
    require(report.get('status') == 'passed' and report.get('current_version') == old['version']
            and report.get('target_version') == proof['version'] and report.get('target_commit') == proof['build_commit']
            and report.get('target_zip_sha256') == proof['files'][stem + '.zip'], 'CASE_PACKAGE_MISMATCH')
    pending = name == 'pending_read'
    require(report.get('original_exe_sha256' if pending else 'old_exe_sha256') == old['exe_sha256']
            and report.get('original_updater_sha256' if pending else 'old_updater_sha256') == old['updater_sha256'], 'CASE_BASELINE_MISMATCH')
    if pending:
        require(report.get('mode') == 'pending_read_preserve_data_install', 'CASE_ROUTE_MISMATCH')
        flags = ['normal_close_used','original_pending_flow_preserved_at_install','original_data_directory_reused',
                 'original_outbox_bytes_preserved','original_flow_completed','stopped_after_recovery','target_ui_confirmed','real_exe_recovery']
    else:
        route, state = name.split('_', 1)
        require(report.get('initial_run_status') == state, 'CASE_STATE_MISMATCH')
        flags = ['original_worker_exited','protected_data_preserved','target_ui_confirmed','paused_intent_and_idle_gate_preserved']
        if route == 'manual':
            require(report.get('mode') == 'preserve_data_manual_install' and report.get('real_settings_button_clicked') is False
                    and report.get('original_updater_used') is False, 'MANUAL_NOT_BUTTON')
            flags += ['normal_close_used','target_program_manifest_verified','original_data_directory_reused']
        else:
            flags += ['real_settings_button_clicked','original_updater_used','actual_backend_download','runtime_threads_alive','immutable_handoff_baseline']
    require(all(report.get(k) is True for k in flags), 'CASE_PROTECTION_INCOMPLETE')


