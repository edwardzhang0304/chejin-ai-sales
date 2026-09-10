import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'ops/formal_release'))
import source_check_reuse as reuse

class SourceCheckReuseTests(unittest.TestCase):
    def setUp(self):
        self.run={'id':34468407574,'head_sha':reuse.ORIGINAL_COMMIT,'head_branch':'codex/gray-release-0.9.x',
          'path':'.github/workflows/worker-windows-package.yml','event':'workflow_dispatch','status':'completed'}
        self.jobs=[{'name':'Build signed formal Windows package','conclusion':'failure','steps':[
          {'name':'Fail fast on native Windows handoff checks','conclusion':'success'},
          {'name':'Run shared source checks','conclusion':'success'}]}]
        self.log="Ran 1108 tests in 929.505s\n\nOK\n# tests 4\n# pass 4\n# fail 0\nAll 47 add_friend package smoke checks passed.\nAttributeError: '_CompatStructuralImage' object has no attribute 'height'\n"

    def test_failed_and_unexecuted_suites_are_never_reused(self):
        completed=reuse.validate_origin(self.run,self.jobs,self.log)
        self.assertIn('unittest',completed)
        self.assertNotIn('run_wechat_win32_ocr_compat_checks.py',completed)
        self.assertNotIn('smoke_e2e',completed)
        for log in (self.log.replace('\nOK\n','\nFAILED\n'),self.log.replace('# fail 0','# fail 1'),self.log.replace('All 47 add_friend package smoke checks passed.','')):
            with self.assertRaises(ValueError):reuse.validate_origin(self.run,self.jobs,log)

    def test_wrong_source_or_failed_stage_rejects_reuse(self):
        with self.assertRaises(ValueError):reuse.validate_origin({**self.run,'head_sha':'a'*40},self.jobs,self.log)
        self.jobs[0]['steps'][0]['conclusion']='failure'
        with self.assertRaises(ValueError):reuse.validate_origin(self.run,self.jobs,self.log)
        for name in ('worker-client/chejin_worker_client/task_runner.py','worker-client/requirements.txt','worker-client/tests/test_task_runner.py','worker-client/omniauto-rpa/.chejin-source.json'):
            self.assertNotIn(name,reuse.REPAIR_FILES)

    def test_same_run_receipt_rejects_tree_commit_and_run_changes(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'GITHUB_RUN_ID':'42','GITHUB_RUN_ATTEMPT':'1'}),patch.object(reuse,'git',return_value=b'b'*40),patch.object(reuse,'fingerprint',return_value='c'*64):
            path=Path(d)/'receipt.json';r=reuse.complete(path,reuse.SUITES)
            self.assertEqual(set(reuse.load_receipt(path)['completed_suites']),set(reuse.SUITES))
            for field,value in (('current_commit','a'*40),('current_tree_sha256','a'*64),('run_id','41'),('run_attempt','2')):
                path.write_text(json.dumps({**r,field:value}))
                with self.assertRaises(ValueError):reuse.load_receipt(path)
            with self.assertRaises(ValueError):reuse.complete(path,['unittest'])

    def test_resume_failure_cannot_create_completion_receipt(self):
        spec=importlib.util.spec_from_file_location('source_check_runner',ROOT/'worker-client/run_checks.py')
        runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
        partial=reuse.validate_origin(self.run,self.jobs,self.log)
        with patch.dict(os.environ,{'CHEJIN_SOURCE_CHECK_RECEIPT':'fixture'}),patch.object(sys,'argv',['run_checks.py','--write-receipt','never.json']),patch.object(reuse,'load_receipt',return_value={'completed_suites':partial}),patch.object(reuse,'complete') as complete,patch.object(runner.subprocess,'run') as run:
            run.return_value.returncode=17
            self.assertEqual(runner.main(),17)
            complete.assert_not_called()
            self.assertEqual(run.call_count,1)
            self.assertTrue(run.call_args.args[0][-1].endswith('run_wechat_win32_ocr_compat_checks.py'))

if __name__=='__main__':unittest.main()
