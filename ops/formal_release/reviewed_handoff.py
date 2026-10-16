"""Verify an owner-approved local engineering handoff without rerunning its tests.

The checked-in handoff contains hashes and test names only. It explicitly states
the original environment; CI attests identity, not a fresh business-test run.
"""
import hashlib
import json
from pathlib import Path
import re
import tomllib

from source_evidence import ROOT, REPO, git, relevant, require

VERSION_FILE = "worker-client/chejin_worker_client/__init__.py"
CONTRACT = "contracts/c2_contract_v3.json"
SCHEMA = "worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/chejin_c2_observation_schema.generated.json"
PROVENANCE = "worker-client/omniauto-rpa/.chejin-source.json"

HOOK_PACKAGING_DELTAS = {
    '.gitattributes', 'worker-client/packaging/start-hook.ps1',
    'worker-client/packaging/Setup-CheJin-Hook.cmd',
    'worker-client/packaging/Refresh-CheJin-Shortcuts.cmd',
    'worker-client/packaging/chejin-updater.spec', 'worker-client/packaging/repair-updater.ps1',
    'worker-client/scripts/test-hook-setup.ps1', 'worker-client/scripts/test-hook-shortcut-repair.ps1',
}

UPDATER_JOB = 'Build only updater repair and exercise original Windows button'
LAUNCHER_JOB = 'Check only Hook launcher directory handoff'
PACKAGING_JOB_PATHS = {
    UPDATER_JOB: {'worker-client/packaging/chejin-updater.spec'},
    LAUNCHER_JOB: HOOK_PACKAGING_DELTAS - {'worker-client/packaging/chejin-updater.spec'},
}
PACKAGING_JOB_INPUTS = {
    UPDATER_JOB: (
        'worker-client/packaging/chejin-updater.spec',
        'worker-client/packaging/chejin_updater_entry.py',
        'worker-client/packaging/updater_runtime_hook.py',
        'worker-client/chejin_worker_client', 'worker-client/omniauto-rpa',
        'contracts', 'worker-client/requirements.txt',
    ),
    LAUNCHER_JOB: tuple(sorted(PACKAGING_JOB_PATHS[LAUNCHER_JOB] | {
        'worker-client/scripts/test-hook-launcher.ps1'})),
}


def normalized_hook(name, raw):
    if name == 'worker-client/packaging/chejin_worker_client_entry.py':
        return re.sub(rb'(?m)^ENTRY_VERSION = "[0-9]+\.[0-9]+\.[0-9]+"', b'ENTRY_VERSION = "RELEASE"', raw)
    if name in {'frontend/README.md', 'backend/README.md'}:
        return re.sub(rb'(?<![0-9])[0-9]+\.[0-9]+\.[0-9]+(?![0-9])', b'RELEASE', raw)
    if name == 'contracts/hook_contract_v1.json':
        value = json.loads(raw); value.pop('product_version', None)
        return json.dumps(value, sort_keys=True).encode()
    if name in {'frontend/package.json', 'frontend/package-lock.json', PROVENANCE}:
        value = json.loads(raw)
        if name == PROVENANCE:
            for key in ('version', 'contract_revision', 'contract_sha256', 'note'):
                value['current_release'].pop(key, None)
        else:
            value.pop('version', None)
            if 'packages' in value: value['packages'][''].pop('version', None)
        return json.dumps(value, sort_keys=True).encode()
    if name == 'backend/pyproject.toml':
        value = tomllib.loads(raw.decode()); value['project'].pop('version', None)
        return json.dumps(value, sort_keys=True).encode()
    if name in {'backend/docker-compose.yml', 'backend/compose.web.yml'}:
        return re.sub(rb'(chejin-(?:api|ops):)[0-9]+\.[0-9]+\.[0-9]+', rb'\1RELEASE', raw)
    return normalized(name, raw)


def validate_hook_origin(data, base):
    """Reuse business evidence only when every source input is accounted for."""
    require(data.get('reuse_mode') in {'release_only', 'reviewed_delta'}, 'HOOK_REVIEWED_REUSE_REQUIRED')
    origin = data['native_origin_commit']
    require(re.fullmatch(r'[0-9a-f]{40}', origin), 'HOOK_ORIGIN_REQUIRED')
    git('merge-base', '--is-ancestor', origin, base)
    names = {n for n in git('ls-tree', '-r', '-z', '--name-only', base).decode().split('\0') if n and relevant(n, 'source')}
    require(set(data['source_sha256']) == names, 'HOOK_FULL_SOURCE_HASH_COVERAGE_REQUIRED')
    packaging_base = data.get('packaging_reviewed_commit', base)
    git('merge-base', '--is-ancestor', origin, packaging_base)
    git('merge-base', '--is-ancestor', packaging_base, base)
    deltas = data['reviewed_packaging_sha256']
    changed = {n for n in git('diff', '-z', '--name-only', origin, packaging_base).decode().split('\0') if n and relevant(n, 'source')}
    require(set(deltas) == changed and changed <= HOOK_PACKAGING_DELTAS, 'HOOK_BUSINESS_OR_DEPENDENCY_CHANGED')
    require(all(hashlib.sha256(git('show', packaging_base + ':' + n)).hexdigest() == digest
                for n, digest in deltas.items()), 'HOOK_PACKAGING_REVIEW_HASH_MISMATCH')
    if data.get('reuse_mode') == 'reviewed_delta':
        approved = data.get('reviewed_delta_sha256', {})
        changed_after = {n for n in git('diff', '-z', '--name-only', packaging_base, base).decode().split('\0')
                         if n and relevant(n, 'source')}
        removed = data.get('reviewed_delta_deleted_sha256', {})
        require(set(approved) | set(removed) == changed_after and (approved or removed)
                and not (set(approved) & set(removed))
                and all(data['source_sha256'].get(n) == digest for n, digest in approved.items())
                and all(n not in names and hashlib.sha256(git('show', packaging_base + ':' + n)).hexdigest() == digest
                        for n, digest in removed.items()),
                'HOOK_REVIEWED_DELTA_NOT_BOUND')
        review = data.get('delta_review', {})
        require(review.get('commit') == base and review.get('result') == 'passed for source push'
                and review.get('thread_id') and review.get('turn_id'), 'HOOK_DELTA_REVIEW_REQUIRED')
    else:
        require(packaging_base == base, 'HOOK_RELEASE_ONLY_BASE_MISMATCH')
    require(data.get('native_source_run') and data.get('packaging_evidence_runs'), 'HOOK_ORIGINAL_EVIDENCE_REQUIRED')
    from source_evidence import resolve_one, api
    receipt = resolve_one(data['native_source_run'], 'source', reference=origin)
    require(receipt['artifact_sha256'] == data['native_source_artifact_sha256'], 'HOOK_SOURCE_ARTIFACT_MISMATCH')
    covered = set()
    for record in data['packaging_evidence_runs']:
        require(record['job'] in PACKAGING_JOB_PATHS, 'HOOK_PACKAGING_JOB_REQUIRED')
        paths = set(record['paths'])
        require(paths and paths <= PACKAGING_JOB_PATHS[record['job']], 'HOOK_PACKAGING_WRONG_JOB_INPUT')
        require(not git('diff', record['commit'], packaging_base, '--',
                        *PACKAGING_JOB_INPUTS[record['job']]).strip(), 'HOOK_PACKAGING_DEPENDENCY_CHANGED')
        run = api('actions/runs/' + str(record['run_id']))
        require(run.get('head_sha') == record['commit'] and run.get('head_branch') == 'gray-release-0.10.x'
                and run.get('status') == 'completed' and run.get('conclusion') == 'success'
                and run.get('path') == '.github/workflows/release-evidence.yml', 'HOOK_PACKAGING_RUN_NOT_PASSED')
        jobs = api('actions/runs/' + str(record['run_id']) + '/jobs?per_page=100')['jobs']
        require(any(j.get('name') == record['job'] and j.get('conclusion') == 'success'
                    and j.get('run_attempt') == run.get('run_attempt') for j in jobs), 'HOOK_PACKAGING_JOB_NOT_PASSED')
        require(set(record['paths']) <= set(deltas) and not git('diff', record['commit'], packaging_base, '--', *record['paths']).strip(),
                'HOOK_PACKAGING_EVIDENCE_INPUT_CHANGED')
        covered.update(record['paths'])
    require(covered == set(deltas), 'HOOK_PACKAGING_EVIDENCE_INCOMPLETE')


def normalized(name, raw):
    if name == VERSION_FILE:
        return re.sub(rb'__version__ = "[0-9]+\.[0-9]+\.[0-9]+"', b'__version__ = "RELEASE"', raw)
    if name in {CONTRACT, SCHEMA}:
        value = json.loads(raw)
        value.pop("contract_revision", None)
        if name == SCHEMA:
            value.pop("contract_sha256", None)
        return json.dumps(value, sort_keys=True).encode()
    return raw


def validate_provenance(old, new, version, contract_sha):
    a, b = json.loads(old), json.loads(new)
    current = b.pop("current_release")
    a.pop("current_release")
    require(current["version"] == current["contract_revision"] == version
            and current["contract_sha256"] == contract_sha, "HANDOFF_PROVENANCE_IDENTITY")
    old_entries = a.pop("selective_integrations")
    new_entries = b.pop("selective_integrations")
    require(new_entries[:len(old_entries)] == old_entries and len(new_entries) == len(old_entries) + 1,
            "HANDOFF_PROVENANCE_HISTORY_CHANGED")
    addition = new_entries[-1]
    require(addition["source_commit"] == current["source_commit"]
            and re.fullmatch(r"[0-9a-f]{40}", addition["source_commit"])
            and addition["scope"] == ["c2_contract_" + version.replace(".", "_") + "_generated_schema"],
            "HANDOFF_NOT_SCHEMA_ONLY_SOURCE")
    a.pop("notes", None); b.pop("notes", None)
    require(a == b, "HANDOFF_PROVENANCE_RUNTIME_CHANGED")


def validate(path):
    path = Path(path)
    require(path.parent == Path("ops/formal_release/handoffs") and path.suffix == ".json", "INVALID_HANDOFF_PATH")
    data = json.loads((ROOT / path).read_text(encoding="utf-8"))
    base = data["reviewed_commit"]
    require(data["schema_version"] == 1 and data["repository"] == REPO and data.get("approval")
            and re.fullmatch(r"[0-9a-f]{40}", base), "UNAPPROVED_HANDOFF")
    git("merge-base", "--is-ancestor", base, "HEAD")
    hook = data.get('transport') == 'hook'
    if hook: validate_hook_origin(data, base)
    for name, expected in data["source_sha256"].items():
        require(hashlib.sha256(git("show", base + ":" + name)).hexdigest() == expected, "HANDOFF_REVIEWED_HASH_MISMATCH")
    version = data["target_version"]
    contract = json.loads(git("show", "HEAD:" + CONTRACT))
    require(contract["contract_revision"] == version, "HANDOFF_TARGET_VERSION_MISMATCH")
    contract_sha = hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    schema = json.loads(git("show", "HEAD:" + SCHEMA))
    require(schema["contract_revision"] == version and schema["contract_sha256"] == contract_sha,
            "HANDOFF_GENERATED_SCHEMA_MISMATCH")
    require(git("show", "HEAD:" + VERSION_FILE).decode().strip() == '__version__ = "' + version + '"',
            "HANDOFF_APPLICATION_VERSION_MISMATCH")
    if hook:
        front = json.loads(git('show', 'HEAD:frontend/package.json'))
        lock = json.loads(git('show', 'HEAD:frontend/package-lock.json'))
        back = tomllib.loads(git('show', 'HEAD:backend/pyproject.toml').decode())
        native = json.loads(git('show', 'HEAD:contracts/hook_contract_v1.json'))
        provenance = json.loads(git('show', 'HEAD:' + PROVENANCE))['current_release']
        require(front['version'] == lock['version'] == lock['packages']['']['version']
                == back['project']['version'] == native['product_version']
                == provenance['version'] == provenance['contract_revision'] == version
                and provenance['contract_sha256'] == contract_sha, 'HOOK_RELEASE_VERSIONS_MISMATCH')
        require(('ENTRY_VERSION = "' + version + '"').encode() in git('show', 'HEAD:worker-client/packaging/chejin_worker_client_entry.py'),
                'HOOK_ENTRY_VERSION_MISMATCH')
        for name, image in (('backend/docker-compose.yml', 'chejin-api:'), ('backend/compose.web.yml', 'chejin-ops:')):
            require((image + version + '}').encode() in git('show', 'HEAD:' + name), 'HOOK_COMPOSE_VERSION_MISMATCH')
    for name in git("diff", "--name-only", "-z", base, "HEAD").decode().split("\0"):
        if not name:
            continue
        if not relevant(name, "source"):
            continue
        old, new = git("show", base + ":" + name), git("show", "HEAD:" + name)
        if hook:
            require(normalized_hook(name, old) == normalized_hook(name, new), 'HANDOFF_BUSINESS_OR_DEPENDENCY_CHANGED: ' + name)
        elif name == PROVENANCE:
            validate_provenance(old, new, version, contract_sha)
        else:
            require(normalized(name, old) == normalized(name, new), "HANDOFF_BUSINESS_OR_DEPENDENCY_CHANGED: " + name)
    total = 0
    unique_cases = set()
    for check in data["checks"]:
        require(re.fullmatch(r"[0-9a-f]{64}", check["original_xml_sha256"])
                and check["count"] == len(check["cases"]) > 0
                and all(c["result"] == "passed" and c["name"] for c in check["cases"]), "HANDOFF_RESULTS_NOT_PASSED")
        total += check["count"]
        unique_cases.update((c.get("classname", ""), c["name"]) for c in check["cases"])
    require(total > 0, "EMPTY_HANDOFF")
    return {"reviewed_commit": base, "checks_reused": len(unique_cases) if data.get("reuse_mode") == "reviewed_delta" else total,
            "report_case_occurrences": total, "checks_reexecuted": 0,
            "historical_baseline_tests": data.get("historical_baseline_tests", 0),
            "handoff_sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), "environment": data["environment"]}
