import json
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reviewed_handoff as handoff


class ReviewedHandoffTests(unittest.TestCase):
    def test_hook_metadata_normalization_preserves_business_and_dependencies(self):
        for name, old, label, business in (
            ('contracts/hook_contract_v1.json', b'{"product_version":"0.10.3","protocol":"hook-v1"}', b'0.10.3', b'hook-v1'),
            ('frontend/package.json', b'{"version":"0.10.3","dependencies":{"react":"19"}}', b'0.10.3', b'19'),
            ('backend/pyproject.toml', b'[project]\nversion = "0.10.3"\nrequires-python = ">=3.11"\n', b'0.10.3', b'3.11'),
        ):
            self.assertEqual(handoff.normalized_hook(name, old), handoff.normalized_hook(name, old.replace(label, b'0.10.4')))
            self.assertNotEqual(handoff.normalized_hook(name, old), handoff.normalized_hook(name, old.replace(business, b'changed')))

    def test_hook_origin_requires_full_coverage_and_unchanged_runtime(self):
        base, origin = 'b'*40, 'a'*40
        runtime, packaging = 'backend/app/main.py', 'worker-client/packaging/chejin-updater.spec'
        data = {'reuse_mode':'release_only','native_origin_commit':origin,'source_sha256':{runtime:'r',packaging:hashlib.sha256(b'p').hexdigest()},
                'reviewed_packaging_sha256':{packaging:hashlib.sha256(b'p').hexdigest()},'native_source_run':'1','native_source_artifact_sha256':'digest',
                'packaging_evidence_runs':[{'run_id':'2','commit':base,'job':handoff.UPDATER_JOB,'paths':[packaging]}]}
        changed = [packaging]
        def fake_git(*args):
            if args[0]=='show': return b'p'
            if args[0]=='ls-tree': return (runtime+'\0'+packaging+'\0').encode()
            if args[:2]==('diff','-z'): return ('\0'.join(changed)+'\0').encode()
            return b''
        def fake_api(path):
            if path.endswith('/jobs?per_page=100'): return {'jobs':[{'name':data['packaging_evidence_runs'][0]['job'],'conclusion':'success','run_attempt':1}]}
            return {'head_sha':base,'head_branch':'gray-release-0.10.x','status':'completed','conclusion':'success','path':'.github/workflows/release-evidence.yml','run_attempt':1}
        with patch.object(handoff,'git',side_effect=fake_git), patch('source_evidence.resolve_one',return_value={'artifact_sha256':'digest'}), patch('source_evidence.api',side_effect=fake_api):
            handoff.validate_hook_origin(data,base)
            missing={**data,'source_sha256':{packaging:'p'}}
            with self.assertRaisesRegex(ValueError,'FULL_SOURCE_HASH_COVERAGE'): handoff.validate_hook_origin(missing,base)
            changed.append(runtime)
            with self.assertRaisesRegex(ValueError,'BUSINESS_OR_DEPENDENCY_CHANGED'): handoff.validate_hook_origin(data,base)
            changed.pop()
            with self.assertRaisesRegex(ValueError,'SOURCE_ARTIFACT_MISMATCH'): handoff.validate_hook_origin({**data,'native_source_artifact_sha256':'changed'},base)
            with self.assertRaisesRegex(ValueError,'PACKAGING_WRONG_JOB_INPUT'):
                handoff.validate_hook_origin({**data,'packaging_evidence_runs':[{**data['packaging_evidence_runs'][0],'paths':[]}]},base)
            with self.assertRaisesRegex(ValueError,'PACKAGING_WRONG_JOB_INPUT'):
                handoff.validate_hook_origin({**data,'packaging_evidence_runs':[{**data['packaging_evidence_runs'][0],'job':handoff.LAUNCHER_JOB}]},base)
            original_git=fake_git
            def dependency_changed(*args):
                if args[:3]==('diff',base,base) and 'worker-client/chejin_worker_client' in args: return b'changed dependency'
                return original_git(*args)
            with patch.object(handoff,'git',side_effect=dependency_changed):
                with self.assertRaisesRegex(ValueError,'PACKAGING_DEPENDENCY_CHANGED'):
                    handoff.validate_hook_origin(data,base)

    def test_contract_version_only_is_ignored_but_business_field_is_protected(self):
        old = b'{"contract_revision":"0.9.75","required":true}'
        self.assertEqual(handoff.normalized(handoff.CONTRACT, old), handoff.normalized(handoff.CONTRACT, old.replace(b"0.9.75", b"0.9.76")))
        self.assertNotEqual(handoff.normalized(handoff.CONTRACT, old), handoff.normalized(handoff.CONTRACT, old.replace(b"true", b"false")))

    def test_dependencies_tests_and_application_changes_are_not_normalized(self):
        for name in ("worker-client/requirements.txt", "backend/app/main.py", "worker-client/tests/test_storage.py"):
            self.assertNotEqual(handoff.normalized(name, b"old"), handoff.normalized(name, b"new"))

    def test_provenance_rejects_rewriting_old_integrations(self):
        old = {"current_release": {}, "selective_integrations": [{"source_commit": "a" * 40}], "runtime": "kept"}
        new = {**old, "current_release": {"version": "0.9.76", "contract_revision": "0.9.76", "contract_sha256": "b" * 64, "source_commit": "c" * 40},
               "selective_integrations": [*old["selective_integrations"], {"source_commit": "c" * 40, "scope": ["c2_contract_0_9_76_generated_schema"]}]}
        handoff.validate_provenance(json.dumps(old), json.dumps(new), "0.9.76", "b" * 64)
        for broken in ({**new, "runtime": "changed"}, {**new, "selective_integrations": new["selective_integrations"][1:]}):
            with self.assertRaises(ValueError): handoff.validate_provenance(json.dumps(old), json.dumps(broken), "0.9.76", "b" * 64)
