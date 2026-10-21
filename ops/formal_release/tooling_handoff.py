"""Compose reviewed, unaffected tool checks with new checks, preserving provenance.

This is not Windows acceptance. The ordinary formal gate still requires the
current Windows job, current input fingerprint and current CI report to pass.
"""
import hashlib
import io
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from source_evidence import ROOT, REPO, WORKFLOW, api, git, relevant, require, validate_report


def inputs(ref, handoff):
    rows = {}
    for row in git('ls-tree', '-rz', ref).split(b'\0'):
        if not row:
            continue
        meta, name = row.decode().split('\t', 1)
        if relevant(name, 'tooling') and name != handoff:
            rows[name] = meta
    return rows


def original(record):
    """Read a successful *component* from its original attempt, not latest retry."""
    run_id, attempt = str(record['run_id']), str(record['attempt'])
    require(all(re.fullmatch(r'[1-9][0-9]*', v) for v in (run_id, attempt)), 'INVALID_TOOL_ORIGIN')
    run = api(f'actions/runs/{run_id}/attempts/{attempt}')
    require(run.get('id') == int(run_id) and run.get('run_attempt') == int(attempt)
            and run.get('head_sha') == record['commit'] and run.get('head_branch') == record['branch']
            and record['branch'] in {'gray-release-0.10.x', 'codex/gray-release-0.9.x'}
            and run.get('event') in {'push', 'workflow_dispatch'} and run.get('status') == 'completed'
            and run.get('repository', {}).get('full_name') == REPO and run.get('path') == WORKFLOW,
            'UNTRUSTED_TOOL_COMPONENT')
    jobs = api(f'actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')['jobs']
    matches = [j for j in jobs if j.get('name') == 'Release tools checks'
               and j.get('run_attempt') == int(attempt) and j.get('conclusion') == 'success']
    require(len(matches) == 1 and matches[0]['id'] == record['job_id'], 'TOOL_COMPONENT_NOT_PASSED')
    artifacts = api(f'actions/runs/{run_id}/artifacts?per_page=100')['artifacts']
    selected = [a for a in artifacts if a.get('name') == f'tooling-evidence-{run_id}-{attempt}'
                and not a.get('expired')]
    require(len(selected) == 1 and selected[0]['id'] == record['artifact_id']
            and selected[0]['size_in_bytes'] < 20_000_000, 'TOOL_COMPONENT_ARTIFACT_INVALID')
    raw = api(f"actions/artifacts/{record['artifact_id']}/zip", binary=True)
    require(hashlib.sha256(raw).hexdigest() == record['artifact_sha256'], 'TOOL_COMPONENT_ARTIFACT_CHANGED')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        def read(name):
            entries = [i for i in archive.infolist() if i.filename == name]
            require(len(entries) == 1 and entries[0].file_size < 10_000_000, 'TOOL_COMPONENT_FILE_INVALID')
            return archive.read(entries[0])
        report = json.loads(read('evidence.json'))
        validate_report(report, run, 'tooling', reference=record['commit'])
        # Chaining reused reports is deliberately unsupported; use original executions.
        require(not report.get('tooling_reuse'), 'TOOL_COMPONENT_MUST_BE_ORIGINAL_EXECUTION')
        groups = []
        for index, scope in enumerate(report['scope']):
            require(set(scope) == {'cwd', 'nodes'}, 'TOOL_COMPONENT_SCOPE_INVALID')
            cases = ET.fromstring(read(f'pytest-{index}.xml')).findall('.//testcase')
            require(cases and all(c.find('failure') is None and c.find('error') is None
                                 and c.find('skipped') is None for c in cases), 'TOOL_COMPONENT_CASE_NOT_PASSED')
            require(report['results'][index]['passed'] == len(cases), 'TOOL_COMPONENT_COUNT_MISMATCH')
            groups.append((scope, cases))
    return groups


def belongs(node, case):
    parts = node.split('::')
    name = case.get('name', '').split('[', 1)[0]
    cls = case.get('classname', '').split('.')
    if Path(parts[0]).stem not in cls:
        return False
    return len(parts) == 1 or (parts[-1] == name and (len(parts) == 2 or parts[-2] == cls[-1]))


def validate(path):
    path = Path(path)
    require(path.parent == Path('ops/formal_release/handoffs') and path.suffix == '.json', 'INVALID_TOOL_HANDOFF_PATH')
    data = json.loads((ROOT / path).read_text())
    require(data.get('schema_version') == 1 and data.get('repository') == REPO, 'INVALID_TOOL_HANDOFF')
    review = data['review']
    require(review.get('result') == 'passed' and review.get('thread_id') and review.get('turn_id')
            and re.fullmatch(r'[0-9a-f]{64}', review.get('evidence_sha256', '')), 'TOOL_REVIEW_REQUIRED')
    ref = data['reviewed_commit']
    scope_path = Path(data['scope_file'])
    require(scope_path.parent == Path('ops/formal_release/handoffs') and scope_path.suffix == '.json'
            and scope_path != path, 'INVALID_TOOL_REVIEW_SCOPE_PATH')
    scope = json.loads(git('show', ref + ':' + str(scope_path)))
    require(set(scope) == {'original', 'changed_inputs', 'pytest', 'unaffected_scope_review'}, 'INVALID_TOOL_REVIEW_SCOPE')
    base = scope['original']['commit']
    require(all(re.fullmatch(r'[0-9a-f]{40}', r) for r in (ref, base)), 'INVALID_TOOL_REVIEW_COMMIT')
    git('merge-base', '--is-ancestor', base, ref)
    git('merge-base', '--is-ancestor', ref, 'HEAD')
    before, reviewed, current = inputs(base, str(path)), inputs(ref, str(path)), inputs('HEAD', str(path))
    require(current == reviewed == data['reviewed_inputs'], 'TOOL_REVIEW_INPUT_CHANGED')
    changed = {n for n in before.keys() | reviewed.keys() if before.get(n) != reviewed.get(n)}
    require(changed and changed == set(scope['changed_inputs']), 'TOOL_REVIEW_DELTA_INCOMPLETE')
    # Each changed input must have an explicit reviewer-approved impact decision.
    for name, impact in scope['changed_inputs'].items():
        require(impact.get('reason') and impact.get('covered_by'), 'TOOL_IMPACT_REVIEW_REQUIRED')
    plan = scope['pytest']
    require(plan and all(set(g) == {'cwd', 'nodes'} and g['nodes'] for g in plan), 'TOOL_SELECTION_REQUIRED')
    fresh = {str(Path(g['cwd']) / node) for g in plan for node in g['nodes']}
    require(all('::' not in node for node in fresh), 'TOOL_RECHECK_FULL_AFFECTED_FILES_REQUIRED')
    allowed = fresh | {'windows-parser', 'gui-driver', 'hook-launcher-setup'}
    require(all(set(v['covered_by']) <= allowed for v in scope['changed_inputs'].values()), 'TOOL_IMPACT_SELECTION_MISSING')
    rows = []
    for group, cases in original(scope['original']):
        assigned = [sum(belongs(n, c) for n in group['nodes']) for c in cases]
        require(all(n == 1 for n in assigned), 'TOOL_ORIGINAL_SCOPE_AMBIGUOUS')
        for node in group['nodes']:
            file = str(Path(group['cwd']) / node.split('::')[0])
            require(before.get(file) == reviewed.get(file) or file in fresh, 'CHANGED_TEST_NOT_REEXECUTED')
            if file in fresh:
                continue
            found = [c for c in cases if belongs(node, c)]
            require(found, 'TOOL_ORIGINAL_CASE_MISSING')
            rows.extend({'cwd': group['cwd'], 'node': node, 'classname': c.get('classname'), 'name': c.get('name'),
                         'result': 'passed', 'mode': 'reused'} for c in found)
    require(rows and scope.get('unaffected_scope_review'), 'TOOL_UNAFFECTED_SCOPE_REVIEW_REQUIRED')
    return {'original': scope['original'], 'review': review, 'cases': rows,
            'count': len(rows), 'pytest': plan, 'reason': scope['unaffected_scope_review'],
            'changed_inputs': scope['changed_inputs'], 'business_tests_executed': 0}
