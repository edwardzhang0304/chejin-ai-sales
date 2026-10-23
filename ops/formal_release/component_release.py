"""One source-derived release scope; no caller-supplied skip flags.

Application versions identify artifacts. Contract revision/hash identify the
wire rules. Single-component releases keep the complete wire contract intact.
This module plans/guards operations; it never marks unexecuted checks passed.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[2]
PLAN_PATH = 'ops/formal_release/component-release.json'
CONTRACT = 'contracts/c2_contract_v3.json'
SCHEMA = 'worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/chejin_c2_observation_schema.generated.json'
WORKER_VERSION = 'worker-client/chejin_worker_client/__init__.py'
ENTRY = 'worker-client/packaging/chejin_worker_client_entry.py'
SCOPES = {'worker', 'admin', 'backend', 'full', 'none'}
STAGES = {
    'worker': ['source_evidence', 'tool_evidence', 'worker_build', 'sign', 'transfer',
               'button_upgrade', 'publish_worker', 'download_verify', 'worker_visual'],
    'admin': ['source_evidence', 'tool_evidence', 'admin_build', 'admin_deploy', 'admin_browser'],
    'backend': ['source_evidence', 'tool_evidence', 'backend_build', 'backend_preflight',
                'maintenance', 'backup', 'backend_deploy', 'backend_health', 'backend_smoke', 'unfreeze'],
    'full': ['source_evidence', 'tool_evidence', 'worker_build', 'sign', 'transfer', 'button_upgrade',
             'admin_build', 'backend_build', 'backend_preflight', 'maintenance', 'backup',
             'backend_deploy', 'admin_deploy', 'backend_health', 'backend_smoke', 'admin_browser',
             'publish_worker', 'download_verify', 'unfreeze', 'worker_visual'],
    'none': [],
}


def require(ok, code):
    if not ok:
        raise ValueError(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root)


def read(root, ref, name):
    return git(root, 'show', f'{ref}:{name}')


def version(value):
    require(isinstance(value, str) and re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value),
            'COMPONENT_VERSION_INVALID')
    return tuple(map(int, value.split('.')))


def assignment(raw, name):
    values = [ast.literal_eval(n.value) for n in ast.parse(raw).body if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)]
    require(len(values) == 1, 'COMPONENT_VERSION_AMBIGUOUS')
    version(values[0])
    return values[0]


def identities(load):
    """Validate identities within each component, not equality across products."""
    worker = assignment(load(WORKER_VERSION), '__version__')
    require(assignment(load(ENTRY), 'ENTRY_VERSION') == worker, 'WORKER_ENTRY_VERSION_MISMATCH')
    front = json.loads(load('frontend/package.json'))
    lock = json.loads(load('frontend/package-lock.json'))
    admin = front['version']
    require(admin == lock['version'] == lock['packages']['']['version'], 'ADMIN_VERSION_MISMATCH')
    backend = tomllib.loads(load('backend/pyproject.toml').decode())['project']['version']
    for value in (admin, backend):
        version(value)
    contract = json.loads(load(CONTRACT))
    revision = contract['contract_revision']; version(revision)
    digest = sha(canonical(contract))
    schema = json.loads(load(SCHEMA))
    require(schema['contract_revision'] == revision and schema['contract_sha256'] == digest,
            'GENERATED_CONTRACT_MISMATCH')
    native = json.loads(load('contracts/hook_contract_v1.json'))
    provenance = json.loads(load('worker-client/omniauto-rpa/.chejin-source.json'))['current_release']
    require(native['product_version'] == revision and provenance['contract_revision'] == revision
            and provenance['contract_sha256'] == digest, 'CONTRACT_PROVENANCE_MISMATCH')
    for file, image, expected in (('backend/docker-compose.yml', 'chejin-api:', backend),
                                  ('backend/compose.web.yml', 'chejin-ops:', admin)):
        require((image + expected + '}').encode() in load(file), 'COMPONENT_COMPOSE_VERSION_MISMATCH')
    return {'worker': worker, 'admin': admin, 'backend': backend,
            'contract_revision': revision, 'contract_sha256': digest}


def owner(name):
    # Test/document/tool-only changes cannot hide runtime files. Unknown paths,
    # shared adapters, dependencies and database/API schemas use the full route.
    if name.startswith(('docs/', 'rules/', 'deliverables/', 'output/', 'ops/formal_release/',
                        '.github/', 'worker-client/tests/', 'backend/tests/')) or name in {'rules.md', 'AGENTS.md'}:
        return None
    if name.startswith(('frontend/src/', 'frontend/public/')) or name in {'frontend/index.html', 'backend/compose.web.yml'}:
        return 'admin'
    if name.startswith('backend/'):
        if name.startswith(('backend/alembic/', 'backend/app/contracts/', 'backend/app/schemas/',
                            'backend/app/models/')) or name in {'backend/Dockerfile', 'backend/requirements.txt', 'backend/requirements.lock'}:
            return 'full'
        return 'backend'
    if name in {WORKER_VERSION, ENTRY, 'worker-client/chejin_worker_client/web_ui.py',
                'worker-client/chejin_worker_client/startup_window_position.py'} or name.startswith((
                    'worker-client/chejin_worker_client/ui/', 'worker-client/chejin_worker_client/web_assets/',
                    'packages/worker-ui-baseline/')):
        return 'worker'
    return 'full'


def make_plan(root, base, target='HEAD'):
    require(re.fullmatch(r'[0-9a-f]{40}', base) is not None, 'EXACT_DEPLOYED_BASE_REQUIRED')
    target = git(root, 'rev-parse', '--verify', target + '^{commit}').decode().strip()
    git(root, 'merge-base', '--is-ancestor', base, target)
    before = identities(lambda n: read(root, base, n))
    after = identities(lambda n: read(root, target, n))
    paths = [n for n in git(root, 'diff', '--no-renames', '--name-only', '-z', base, target).decode().split('\0') if n]
    def affected(name):
        if name in {'frontend/package.json', 'frontend/package-lock.json'}:
            # Worker esbuild/React resolve from frontend/node_modules. Only the
            # application's version fields are independent metadata.
            def dependencies(ref):
                obj = json.loads(read(root, ref, name)); obj.pop('version', None)
                if name.endswith('package-lock.json'):
                    obj.get('packages', {}).get('', {}).pop('version', None)
                return obj
            return 'admin' if dependencies(base) == dependencies(target) else 'full'
        return owner(name)
    changed = {affected(n) for n in paths} - {None}
    scope = next(iter(changed)) if len(changed) == 1 else ('full' if changed else 'none')
    if before['contract_sha256'] != after['contract_sha256']:
        scope = 'full'
    for component in ('worker', 'admin', 'backend'):
        if scope == component:
            require(version(after[component]) > version(before[component]), 'CHANGED_COMPONENT_VERSION_NOT_NEWER')
        elif scope != 'full':
            require(after[component] == before[component], 'UNCHANGED_COMPONENT_VERSION_CHANGED')
    # Include both versions of changed files, including deletions and mode changes.
    diff_sha = sha(git(root, 'diff', '--no-ext-diff', '--binary', '--no-renames', base, target))
    return {'schema_version': 1, 'base_commit': base, 'source_commit': target,
            'scope': scope, 'before': before, 'after': after, 'changed_paths': paths,
            'diff_sha256': diff_sha, 'stages': STAGES[scope],
            'contract_policy': 'unchanged_exact' if scope != 'full' else 'coordinated_review',
            'button_first': scope in {'worker', 'full'}, 'checks_skipped': [],
            'physical_acceptance': 'not_run'}


def validate_plan(plan, root=ROOT):
    require(isinstance(plan, dict), 'COMPONENT_PLAN_INVALID')
    expected = make_plan(root, plan.get('base_commit', ''), plan.get('source_commit', ''))
    require(plan == expected, 'COMPONENT_PLAN_CHANGED_OR_FORGED')
    return expected


def load_plan(path, root=ROOT):
    return validate_plan(json.loads(Path(path).read_text()), root)


def require_stage(plan, stage, root=ROOT):
    validate_plan(plan, root)
    require(stage in plan['stages'], 'STAGE_OUTSIDE_COMPONENT_SCOPE')


def environment_stage(stage, root=ROOT):
    path = os.environ.get('CHEJIN_COMPONENT_PLAN')
    if path:
        plan = load_plan(path, root)
        require(git(root, 'rev-parse', 'HEAD').decode().strip() == plan['source_commit'], 'COMPONENT_CHECKOUT_CHANGED')
        require_stage(plan, stage, root)
        # Candidate bytes must match the reviewed commit, including untracked
        # build inputs; a plan from another checkout cannot bless local edits.
        dirty = git(root, 'status', '--porcelain', '--untracked-files=all', '-z')
        require(not dirty, 'COMPONENT_CHECKOUT_DIRTY')
        return plan
    return None  # Existing coordinated/OCR route retains its existing gates.


def require_live_baseline(plan, live):
    """Caller supplies a fresh operator observation, never a CI's guessed state.

    Components may have different source commits after an independent release;
    the baseline receipt must pin all three versions and the exact wire contract.
    """
    require(isinstance(live, dict) and live.get('healthy') is True, 'LIVE_BACKEND_NOT_HEALTHY')
    require(all(live.get(k) == v for k, v in plan['before'].items()), 'LIVE_BASELINE_CHANGED')


def validate_source_versions(root=ROOT):
    return identities(lambda n: (Path(root) / n).read_bytes())


def operator_plan(approval, scope):
    """Validate a root-owned operator receipt, NOT a package-supplied assertion.

    The local operator prepares it with validate_plan against Git. Server
    consumption checks the sealed identity and fixed stage policy again.
    """
    plan = approval.get('component_plan', {})
    require(plan.get('schema_version') == 1 and plan.get('scope') == scope
            and scope in {'worker', 'backend', 'admin'}
            and plan.get('stages') == STAGES[scope]
            and plan.get('contract_policy') == 'unchanged_exact', 'OPERATOR_COMPONENT_PLAN_INVALID')
    require(approval.get('component_plan_sha256') == sha(canonical(plan)), 'OPERATOR_PLAN_HASH_MISMATCH')
    for field in ('base_commit', 'source_commit'):
        require(re.fullmatch(r'[0-9a-f]{40}', plan.get(field, '')) is not None, 'OPERATOR_COMMIT_INVALID')
    require(re.fullmatch(r'[0-9a-f]{64}', plan.get('diff_sha256', '')) is not None, 'OPERATOR_DIFF_INVALID')
    before, after = plan['before'], plan['after']
    for field in ('contract_revision', 'contract_sha256'):
        require(before[field] == after[field], 'SINGLE_COMPONENT_CONTRACT_CHANGED')
    for component in ('worker', 'admin', 'backend'):
        require((version(after[component]) > version(before[component])) if component == scope
                else after[component] == before[component], 'COMPONENT_VERSION_BOUNDARY_CHANGED')
    require_live_baseline(plan, approval['live_baseline'])
    return plan


def worker_publication_approval(approval, meta):
    plan = operator_plan(approval, 'worker')
    require(meta['version'] == plan['after']['worker'] and meta['commit'] == plan['source_commit'],
            'COMPONENT_PACKAGE_IDENTITY_MISMATCH')
    # Reuse the existing upgrade protection validator, with the original EXEs
    # pinned by the operator's checked old build receipt, not by the reports.
    from upgrade_evidence import validate_case
    old = approval['old_client']
    require(old['version'] == meta['current_version'] == plan['before']['worker'], 'UPGRADE_START_MISMATCH')
    for field in ('version', 'zip_sha256', 'exe_sha256', 'updater_sha256'):
        require(old.get(field) == approval['live_baseline'].get('worker_baseline', {}).get(field)
                and old.get(field) is not None, 'UPGRADE_BASELINE_IDENTITY_MISMATCH')
        if field != 'version':
            require(re.fullmatch(r'[0-9a-f]{64}', old[field]) is not None, 'UPGRADE_BASELINE_HASH_INVALID')
    proof = {'version': meta['version'], 'build_commit': meta['commit'],
             'files': {f"chejin-worker-v{meta['version']}-windows-x64.zip": meta['sha256']}}
    reports = approval['upgrade_reports']
    require(set(reports) == {'paused', 'faulted'}, 'UPGRADE_CASES_REQUIRED')
    for state, report in reports.items():
        validate_case('button_' + state, report, {'old_client': old}, proof)
        require(report.get('hook_bound_upgrade') is True, 'HOOK_BINDING_PROTECTION_REQUIRED')
    return plan


def prepare_worker_approval(plan, descriptor, old_client, reports, live, root=ROOT):
    """Compose already authenticated operator/CI inputs; never invent results."""
    require_stage(plan, 'publish_worker', root)
    require(descriptor.get('channel') == 'gray-hook', 'HOOK_ONLY')
    approval = {'version': descriptor['version'], 'commit': descriptor['git_commit'],
                'sha256': descriptor['artifact_sha256'], 'current_version': old_client['version'],
                'component_plan': plan, 'component_plan_sha256': sha(canonical(plan)),
                'old_client': old_client, 'upgrade_reports': reports, 'live_baseline': live}
    worker_publication_approval(approval, approval)
    return approval


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('plan'); p.add_argument('--base', required=True); p.add_argument('--target', default='HEAD')
    p.add_argument('--expect', choices=sorted(SCOPES), required=True); p.add_argument('--output', type=Path, required=True)
    p = commands.add_parser('check-stage'); p.add_argument('--plan', type=Path, required=True); p.add_argument('--stage', required=True)
    p = commands.add_parser('worker-approval')
    for name in ('plan', 'descriptor', 'old-client', 'paused-report', 'faulted-report', 'live-baseline', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    p = commands.add_parser('versions'); p.add_argument('--coordinated', action='store_true')
    args = parser.parse_args()
    if args.command == 'plan':
        result = make_plan(ROOT, args.base, args.target)
        require(result['scope'] == args.expect, 'SCOPE_DIFFERS_FROM_REQUEST')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical(result) + b'\n')
    elif args.command == 'check-stage':
        result = load_plan(args.plan); require_stage(result, args.stage)
    elif args.command == 'worker-approval':
        def get(path): return json.loads(path.read_text(encoding='utf-8-sig'))
        result = prepare_worker_approval(load_plan(args.plan), get(args.descriptor), get(args.old_client),
                                        {'paused': get(args.paused_report), 'faulted': get(args.faulted_report)},
                                        get(args.live_baseline))
        args.output.write_bytes(canonical(result) + b'\n')
    else:
        result = validate_source_versions()
        if args.coordinated:
            require(len({result[k] for k in ('worker', 'backend', 'admin', 'contract_revision')}) == 1,
                    'COORDINATED_RELEASE_VERSIONS_MISMATCH')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
