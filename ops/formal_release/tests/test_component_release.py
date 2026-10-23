import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ops/formal_release'))
import component_release as cr
import source_evidence as se
import receiver
import publish_hook


def put(root, path, value):
    p = root / path; p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value) if isinstance(value, dict) else value)


def commit(root):
    cr.git(root, 'add', '.')
    cr.git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
    return cr.git(root, 'rev-parse', 'HEAD').decode().strip()


@pytest.fixture
def repo(tmp_path):
    cr.git(tmp_path, 'init', '-q')
    for path in (cr.WORKER_VERSION, cr.ENTRY, 'frontend/package.json', 'frontend/package-lock.json',
                 'backend/pyproject.toml', cr.CONTRACT, cr.SCHEMA, 'contracts/hook_contract_v1.json',
                 'worker-client/omniauto-rpa/.chejin-source.json', 'backend/docker-compose.yml', 'backend/compose.web.yml'):
        put(tmp_path, path, (ROOT / path).read_text())
    put(tmp_path, 'worker-client/chejin_worker_client/web_assets/worker-ui.css', 'body { padding: 4px; }')
    put(tmp_path, 'backend/app/services/example.py', 'value = 1')
    put(tmp_path, 'frontend/src/example.ts', 'export const value = 1;')
    return tmp_path, commit(tmp_path)


def bump(root, part):
    if part == 'worker':
        for file in (cr.WORKER_VERSION, cr.ENTRY):
            put(root, file, (root / file).read_text().replace('0.10.13', '0.10.14'))
    elif part == 'backend':
        file = 'backend/pyproject.toml'; put(root, file, (root / file).read_text().replace('0.10.13', '0.10.14'))
        file = 'backend/docker-compose.yml'; put(root, file, (root / file).read_text().replace('0.10.13', '0.10.14'))
    else:
        for file in ('frontend/package.json', 'frontend/package-lock.json'):
            data = json.loads((root / file).read_text()); data['version'] = '0.10.14'
            if 'packages' in data: data['packages']['']['version'] = '0.10.14'
            put(root, file, data)
        file = 'backend/compose.web.yml'; put(root, file, (root / file).read_text().replace('0.10.13', '0.10.14'))


@pytest.mark.parametrize('part,path', [
    ('worker', 'worker-client/chejin_worker_client/web_assets/worker-ui.css'),
    ('backend', 'backend/app/services/example.py'), ('admin', 'frontend/src/example.ts'),
])
def test_real_git_diff_selects_only_changed_component(repo, part, path):
    root, base = repo
    put(root, path, 'changed'); bump(root, part); target = commit(root)
    plan = cr.make_plan(root, base, target)
    assert plan['scope'] == part
    assert plan['after']['contract_sha256'] == plan['before']['contract_sha256']
    assert plan['after']['contract_revision'] == '0.10.13'
    cr.validate_plan(plan, root)
    if part == 'worker':
        assert {'button_upgrade', 'publish_worker', 'worker_visual'} <= set(plan['stages'])
        for forbidden in ('backend_deploy', 'admin_deploy', 'maintenance', 'admin_browser'):
            with pytest.raises(ValueError, match='STAGE_OUTSIDE'): cr.require_stage(plan, forbidden, root)
    else:
        with pytest.raises(ValueError, match='STAGE_OUTSIDE'): cr.require_stage(plan, 'worker_build', root)


@pytest.mark.parametrize('path', ['contracts/new-rule.json', 'backend/alembic/versions/new.py',
    'backend/app/schemas/worker.py', 'worker-client/chejin_worker_client/storage.py',
    'worker-client/omniauto-rpa/apps/shared.py', 'worker-client/requirements.txt', 'unknown/runtime.py'])
def test_shared_protocol_data_and_unknown_changes_cannot_take_short_path(repo, path):
    root, base = repo; put(root, path, 'changed'); commit(root)
    assert cr.make_plan(root, base)['scope'] == 'full'


def test_change_only_contract_label_still_requires_coordinated_release(repo):
    root, base = repo
    contract = json.loads((root / cr.CONTRACT).read_text()); contract['contract_revision'] = '0.10.14'
    schema = json.loads((root / cr.SCHEMA).read_text())
    schema.update(contract_revision='0.10.14', contract_sha256=cr.sha(cr.canonical(contract)))
    put(root, cr.CONTRACT, contract); put(root, cr.SCHEMA, schema); commit(root)
    with pytest.raises(ValueError, match='CONTRACT_PROVENANCE'): cr.make_plan(root, base)
    hook = json.loads((root / 'contracts/hook_contract_v1.json').read_text()); hook['product_version'] = '0.10.14'
    put(root, 'contracts/hook_contract_v1.json', hook)
    provenance = json.loads((root / 'worker-client/omniauto-rpa/.chejin-source.json').read_text())
    provenance['current_release'].update(contract_revision='0.10.14', contract_sha256=schema['contract_sha256'])
    put(root, 'worker-client/omniauto-rpa/.chejin-source.json', provenance); commit(root)
    assert cr.make_plan(root, base)['scope'] == 'full'


def test_new_worker_version_requires_matching_entry_and_never_contract_relabel(repo):
    root, base = repo
    put(root, cr.WORKER_VERSION, '__version__ = "0.10.14"\n'); commit(root)
    with pytest.raises(ValueError, match='ENTRY_VERSION_MISMATCH'): cr.make_plan(root, base)


def test_forged_stage_list_scope_and_source_are_rejected(repo):
    root, base = repo; bump(root, 'worker'); commit(root)
    plan = cr.make_plan(root, base)
    for field, value in [('scope', 'backend'), ('stages', ['publish_worker']), ('source_commit', base), ('diff_sha256', '0' * 64)]:
        with pytest.raises(ValueError, match='PLAN_CHANGED'): cr.validate_plan({**plan, field: value}, root)
    with pytest.raises(ValueError, match='LIVE_BASELINE'): cr.require_live_baseline(plan, {**plan['before'], 'healthy': True, 'backend': '99.0.0'})


def approval_for(plan):
    meta = {'version': plan['after']['worker'], 'commit': plan['source_commit'], 'sha256': 'f' * 64,
            'current_version': plan['before']['worker']}
    old = {'version': meta['current_version'], 'exe_sha256': 'd' * 64, 'updater_sha256': 'e' * 64, 'zip_sha256': 'c' * 64}
    reports = {}
    for state in ('paused', 'faulted'):
        reports[state] = {'status': 'passed', 'initial_run_status': state, 'current_version': old['version'],
                          'target_version': meta['version'], 'target_commit': meta['commit'], 'target_zip_sha256': meta['sha256'],
                          'old_exe_sha256': old['exe_sha256'], 'old_updater_sha256': old['updater_sha256'],
                          **dict.fromkeys(['original_worker_exited', 'protected_data_preserved', 'target_ui_confirmed',
                              'paused_intent_and_idle_gate_preserved', 'real_settings_button_clicked', 'original_updater_used',
                              'actual_backend_download', 'runtime_threads_alive', 'immutable_handoff_baseline', 'hook_bound_upgrade'], True)}
    return {**meta, 'component_plan': plan, 'component_plan_sha256': cr.sha(cr.canonical(plan)),
            'live_baseline': {**plan['before'], 'healthy': True, 'worker_baseline': dict(old)}, 'old_client': old, 'upgrade_reports': reports}, meta


def test_production_worker_gate_uses_original_upgrade_protections_without_admin_or_maintenance(repo, monkeypatch):
    root, base = repo; bump(root, 'worker'); commit(root)
    approval, meta = approval_for(cr.make_plan(root, base))
    put(root, 'production-approval.json', approval)
    import maintenance
    monkeypatch.setattr(maintenance, 'validate_ingress', lambda: None)  # No real Docker/nginx in this test.
    monkeypatch.setattr(maintenance, 'SNIPPET', root / 'absent-maintenance')
    receiver.require_publication_approval(root, meta)
    for field in ('original_updater_used', 'protected_data_preserved', 'immutable_handoff_baseline', 'hook_bound_upgrade'):
        broken = copy.deepcopy(approval); broken['upgrade_reports']['faulted'][field] = False
        put(root, 'production-approval.json', broken)
        with pytest.raises(ValueError): receiver.require_publication_approval(root, meta)


def test_tool_fingerprint_reuses_release_parameters_but_not_executable_changes(repo):
    root, base = repo
    put(root, 'ops/formal_release/tool.py', 'print(1)'); base = commit(root)
    bump(root, 'worker'); put(root, 'ops/formal_release/handoffs/new.json', {'release': '0.10.14'})
    target = commit(root)
    assert se.fingerprint(base, 'tooling', root) == se.fingerprint(target, 'tooling', root)
    put(root, cr.WORKER_VERSION, (root / cr.WORKER_VERSION).read_text() + '\nraise RuntimeError()\n'); changed = commit(root)
    assert se.fingerprint(target, 'tooling', root) != se.fingerprint(changed, 'tooling', root)
    put(root, 'ops/formal_release/tool.py', 'print(2)'); changed2 = commit(root)
    assert se.fingerprint(changed, 'tooling', root) != se.fingerprint(changed2, 'tooling', root)


def test_fixed_hook_publish_does_not_restart_services_or_deploy_admin(repo, monkeypatch):
    root, base = repo; bump(root, 'worker'); commit(root)
    approval, meta = approval_for(cr.make_plan(root, base))
    put(root, 'production-approval.json', approval)
    put(root, 'release.json', {'channel': 'gray-hook', 'version': meta['version'], 'git_commit': meta['commit'], 'artifact_sha256': meta['sha256']})
    put(root, 'public-keys.json', '{}')
    approvals = root / 'approvals'; approvals.mkdir()
    put(approvals, meta['sha256'] + '.json', approval)
    monkeypatch.setattr(publish_hook, 'trusted_path', lambda p: Path(p))  # isolated non-root test directory
    monkeypatch.setattr(publish_hook, 'verify_hook', lambda *a: {k: approval['component_plan']['after'][k] for k in ('contract_revision', 'contract_sha256')})  # verified separately using real signed ZIPs
    monkeypatch.setattr(publish_hook, 'digest', lambda _: meta['sha256'])
    monkeypatch.setattr(publish_hook, 'require_publication_approval', lambda *a, **kw: cr.worker_publication_approval(approval, meta))
    package = root / 'package'; package.mkdir()
    for name in ('release.json', 'public-keys.json'): (package / name).write_bytes((root / name).read_bytes())
    calls = []
    def execute(args, **kw):
        calls.append((args, kw))
        if 'inspect' in args: return 'healthy'
        if '-' in args: return '{"ok":true,"publication":"published"}'
        return ''
    monkeypatch.setattr(publish_hook, 'run_fixed', execute)
    result = publish_hook.publish(root / 'package', {'component_approvals': str(approvals), 'public_keys': str(root / 'public-keys.json'), 'container': 'test-api'})
    assert result['backend_deployed'] is False and result['admin_deployed'] is False
    register = next((args, kw) for args, kw in calls if '-' in args)
    assert '0.10.13' in register[0]  # Contract retained, package is 0.10.14.
    assert 'assert_release_ready(db)' in register[1]['input']
    assert all(not {'restart', 'compose', 'systemctl', 'alembic'} & set(args) for args, _ in calls)


def test_backend_unfreeze_requires_preservation_and_business_checks_but_not_admin_receipt(repo):
    import maintenance
    root, base = repo; bump(root, 'backend'); commit(root)
    plan = cr.make_plan(root, base)
    approval = {'component_plan': plan, 'component_plan_sha256': cr.sha(cr.canonical(plan)),
                'live_baseline': {**plan['before'], 'healthy': True}, 'backend_ready': True, 'workers_safe': True,
                'deployed_source_commit': plan['source_commit'], 'backend_smoke': 'passed', 'data_preserved': True}
    maintenance.validate_unfreeze(approval)
    for field, value in [('workers_safe', False), ('data_preserved', False), ('backend_smoke', 'failed'),
                         ('deployed_source_commit', base)]:
        with pytest.raises((ValueError, AssertionError)): maintenance.validate_unfreeze({**approval, field: value})


def test_original_and_target_downloads_start_together_and_failure_stops_acceptance(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    import updater_repair
    barrier = threading.Barrier(2)
    started = []
    def fetch(run, folder):
        started.append(run); barrier.wait(timeout=5)
        raise ValueError('HASH_MISMATCH_' + run)
    monkeypatch.setattr(updater_repair, 'fetch_package', fetch)
    args = SimpleNamespace(root=tmp_path / 'download', old_run='old', target_run='target')
    with pytest.raises(ValueError, match='HASH_MISMATCH_old'): updater_repair.prepare(args)
    assert set(started) == {'old', 'target'}
    assert not (args.root / 'plan.json').exists()


def test_small_build_identity_binds_signing_hash_before_server_download(monkeypatch):
    import io
    import zipfile
    import sign_existing_hook as sign
    run = {'id': 123, 'head_sha': 'a' * 40}
    identity = {'version': '0.10.14', 'source_commit': run['head_sha'], 'build_run': '123', 'package_sha256': 'b' * 64}
    def archive(value):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z: z.writestr('package-identity.json', json.dumps(value))
        return buffer.getvalue()
    raw = archive(identity)
    item = {'id': 10, 'name': 'hook-package-identity', 'expired': False, 'size_in_bytes': len(raw), 'digest': 'sha256:' + cr.sha(raw)}
    monkeypatch.setattr(sign, 'api', lambda *a, **kw: raw)
    assert sign.build_package_hash(run, [item], '0.10.14') == 'b' * 64
    for bad in ({**run, 'id': 124}, {**run, 'head_sha': 'c' * 40}):
        with pytest.raises(ValueError, match='IDENTITY_MISMATCH'): sign.build_package_hash(bad, [item], '0.10.14')
    with pytest.raises(ValueError, match='DIGEST_MISMATCH'):
        sign.build_package_hash(run, [{**item, 'digest': 'sha256:' + 'd' * 64}], '0.10.14')


def test_reviewed_handoff_component_path_consumes_real_source_versions(repo, monkeypatch):
    import reviewed_handoff as h
    root, base = repo
    put(root, 'worker-client/chejin_worker_client/web_assets/worker-ui.css', 'body { padding: 20px; }')
    reviewed = commit(root)
    paths = cr.git(root, 'ls-tree', '-r', '--name-only', reviewed).decode().splitlines()
    hashes = {p: cr.sha(cr.read(root, reviewed, p)) for p in paths}
    bump(root, 'worker'); commit(root)
    data = {'schema_version': 1, 'repository': h.REPO, 'approval': 'fixture source audit',
            'reviewed_commit': reviewed, 'transport': 'hook', 'component_base_commit': base,
            'target_version': '0.10.14', 'source_sha256': hashes, 'environment': 'synthetic Git only',
            'checks': [{'original_xml_sha256': 'a' * 64, 'count': 1, 'cases': [{'result': 'passed', 'name': 'fixture'}]}]}
    path = 'ops/formal_release/handoffs/fixture.json'; put(root, path, data)
    monkeypatch.setattr(h, 'ROOT', root)
    monkeypatch.setattr(h, 'git', lambda *args: cr.git(root, *args))
    monkeypatch.setattr(h, 'validate_hook_origin', lambda *a: None)  # Network provenance tested by existing suite.
    assert h.validate(path)['checks_reexecuted'] == 0
    data.pop('component_base_commit'); put(root, path, data)
    with pytest.raises(ValueError, match='TARGET_VERSION_MISMATCH'): h.validate(path)


@pytest.mark.parametrize('name', ['frontend/package.json', 'frontend/package-lock.json', 'frontend/.npmrc', 'frontend/vite.config.ts'])
def test_shared_build_inputs_do_not_take_admin_shortcut(repo, name):
    root, base = repo; bump(root, 'admin')
    if name.endswith('.json'):
        obj = json.loads((root / name).read_text())
        target = obj if name.endswith('package.json') else obj['packages']['']
        target.setdefault('dependencies', {})['react'] = '99.0.0'
        put(root, name, obj)
    else: put(root, name, 'changed')
    commit(root)
    assert cr.make_plan(root, base)['scope'] == 'full'


def test_upgrade_proof_cannot_substitute_old_version_or_original_binary(repo):
    root, base = repo; bump(root, 'worker'); commit(root)
    approval, meta = approval_for(cr.make_plan(root, base))
    cr.worker_publication_approval(approval, meta)
    for field, value in [('version', '0.10.11'), ('exe_sha256', 'a'*64), ('updater_sha256', 'b'*64), ('zip_sha256', 'd'*64)]:
        changed = copy.deepcopy(approval); changed['old_client'][field] = value
        with pytest.raises(ValueError): cr.worker_publication_approval(changed, meta)
    changed = copy.deepcopy(approval); changed['upgrade_reports']['faulted']['target_zip_sha256'] = 'a'*64
    with pytest.raises(ValueError): cr.worker_publication_approval(changed, meta)


def test_actual_build_checkout_must_match_plan_and_be_clean(repo, monkeypatch, tmp_path):
    root, base = repo; bump(root, 'admin'); commit(root); plan = cr.make_plan(root, base)
    # Plan outside the source root, so the evidence itself does not dirty source.
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'plan.json'; put(path.parent, path.name, plan)
        monkeypatch.setenv('CHEJIN_COMPONENT_PLAN', str(path))
        assert cr.environment_stage('admin_build', root) == plan
        put(root, 'frontend/src/example.ts', 'unreviewed')
        with pytest.raises(ValueError, match='DIRTY'): cr.environment_stage('admin_build', root)
        cr.git(root, 'checkout', '--', 'frontend/src/example.ts')
        put(root, 'frontend/unreviewed.ts', 'untracked')
        with pytest.raises(ValueError, match='DIRTY'): cr.environment_stage('admin_build', root)
        (root/'frontend/unreviewed.ts').unlink()
        cr.git(root, 'checkout', '--detach', base)
        with pytest.raises(ValueError, match='CHECKOUT_CHANGED'): cr.environment_stage('admin_build', root)


def test_admin_main_checks_actual_source_before_npm_and_records_identity(repo, monkeypatch):
    import tempfile
    import build_admin
    root, base = repo
    put(root,'.gitignore','frontend/dist/\n'); base=commit(root)
    bump(root,'admin'); commit(root); plan=cr.make_plan(root,base)
    calls=[]
    real_run = subprocess.run
    def npm(*args,**kwargs):
        if args[0][0] != 'npm': return real_run(*args, **kwargs)
        calls.append(kwargs['cwd'])
        put(root,'frontend/dist/index.html','fixture')
        put(root,'frontend/dist/assets/app.js','const config={VITE_API_BASE_URL:"/api"};')
    monkeypatch.setattr(build_admin.subprocess,'run',npm)
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'plan.json'; put(path.parent,path.name,plan)
        monkeypatch.setenv('CHEJIN_COMPONENT_PLAN',str(path))
        output=Path(temp)/'candidate.json'
        monkeypatch.setattr(sys,'argv',['build_admin','--environment','production','--api-base','/api',
                                       '--source-root',str(root),'--output',str(output)])
        build_admin.main()
        assert json.loads(output.read_text())['source_commit']==plan['source_commit']
        put(root,'frontend/src/example.ts','unreviewed')
        with pytest.raises(ValueError,match='DIRTY'):build_admin.main()
        cr.git(root,'checkout','--','frontend/src/example.ts')
        cr.git(root,'checkout','--detach',base)
        with pytest.raises(ValueError,match='CHECKOUT_CHANGED'):build_admin.main()
        assert calls==[root/'frontend']
