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

class SharedPrefixReuseTests(unittest.TestCase):
    def test_prefix_reuses_only_completed_native_and_shared_steps(self):
        run={'id':reuse.PREFIX_RUN,'head_sha':reuse.PREFIX_COMMIT,'head_branch':'codex/gray-release-0.9.x','path':'.github/workflows/worker-windows-package.yml','event':'workflow_dispatch','status':'completed'}
        jobs=[{'name':'Build signed formal Windows package','conclusion':'failure','steps':[{'name':name,'conclusion':'success'} for name in ('Fail fast on native Windows handoff checks','Native Windows long-path negative and repaired controls')]}]
        log='end-action id=__self.__run;outcome=success;conclusion=success;\nend-action id=__self.__run_2;outcome=success;conclusion=success;\ntest_v16_component_ui_assets_are_packaged\nFAILED (failures=1)\nend-action id=__self.__run_3;outcome=failure;conclusion=failure;'
        self.assertEqual(reuse.validate_prefix(run,jobs,log),['credentials'])
        for bad in (log.replace('__run;outcome=success','__run;outcome=failure'),log.replace('__run_2;outcome=success','__run_2;outcome=failure'),log.replace('FAILED (failures=1)','FAILED (errors=1)')):
            with self.assertRaises(ValueError):reuse.validate_prefix(run,jobs,bad)
        with self.assertRaises(ValueError):reuse.validate_prefix({**run,'head_sha':'a'*40},jobs,log)
        for path in ('worker-client/chejin_worker_client/update.py','worker-client/tests/test_update_long_paths.py','worker-client/requirements.txt','backend/app/services/task_service.py'):
            self.assertNotIn(path,reuse.PREFIX_REPAIRS)

    def test_reused_commands_cannot_change_and_need_no_optional_yaml(self):
        workflow='.github/workflows/worker-windows-package.yml';action='.github/actions/worker-release-checks/action.yml'
        # This validator is archival; the new formal workflow uses generic evidence.
        old_w=reuse.git('show',reuse.PREFIX_COMMIT+':'+workflow).decode();new_w=reuse.git('show','cd8763ed38ec1df1ff8054e49c3381e8a5322f62:'+workflow).decode()
        old_a=reuse.git('show',reuse.PREFIX_COMMIT+':'+action).decode();new_a=(ROOT/action).read_text()
        reuse.validate_prefix_commands(old_w,new_w,old_a,new_a)
        with self.assertRaises(ValueError):reuse.validate_prefix_commands(old_w,new_w.replace('3.12.10','3.13.0'),old_a,new_a)
        with self.assertRaises(ValueError):reuse.validate_prefix_commands(old_w,new_w,old_a,new_a.replace('python -m pytest','echo bypass'))

    def test_later_shared_chain_rejects_failed_group_and_changed_recovery_tests(self):
        run={'id':reuse.LATER_RUN,'head_sha':reuse.LATER_COMMIT,'head_branch':'codex/gray-release-0.9.x','path':'.github/workflows/worker-windows-package.yml','event':'workflow_dispatch','status':'completed'}
        jobs=[{'name':'Build signed formal Windows package','conclusion':'failure','steps':[{'name':'Resolve immutable completed source-check evidence','conclusion':'success'}]}]
        log='\n'.join('end-action id=__self.__run_'+str(i)+';outcome=success;conclusion=success;' for i in (3,4,5))+'\ntest_slot_ledger_contract_separates_fact_scope_from_delivery\nend-action id=__self.__run_6;outcome=failure;conclusion=failure;'
        reuse.validate_later_shared(run,jobs,log)
        for i in (3,4,5):
            with self.assertRaises(ValueError):reuse.validate_later_shared(run,jobs,log.replace('__run_'+str(i)+';outcome=success','__run_'+str(i)+';outcome=failure'))
        self.assertNotIn('worker-client/tests/test_ui_contract.py',reuse.LATER_REPAIRS)
        self.assertNotIn('backend/app/services/c3_service.py',reuse.LATER_REPAIRS)

class FailedUnitResumeTests(unittest.TestCase):
    def test_only_exact_failed_methods_are_retried(self):
        run={'id':reuse.UNIT_RUN,'head_sha':reuse.UNIT_COMMIT,'head_branch':'codex/gray-release-0.9.x','path':'.github/workflows/worker-windows-package.yml','event':'workflow_dispatch','status':'completed'}
        jobs=[{'name':'Build signed formal Windows package','conclusion':'failure','steps':[{'name':n,'conclusion':'success'} for n in ('Resolve immutable completed source-check evidence','Fail fast on native Windows handoff checks','Run shared source checks')]}]
        headers=[f'ERROR: {reuse.UNIT_RETRY[0]} (test_release_gate_runner.ReleaseGateRunnerTest.{reuse.UNIT_RETRY[0]}) (exit_code={i})' for i in range(1,6)]
        headers += [f'ERROR: {n} (test_release_gate_runner.ReleaseGateRunnerTest.{n})' for n in reuse.UNIT_RETRY[1:3]]
        headers += [f'FAIL: {reuse.UNIT_RETRY[3]} (test_incident_evidence.IncidentEvidenceTest.{reuse.UNIT_RETRY[3]})']
        log='\n'.join(headers+['Ran 1108 tests in 804.266s','FAILED (failures=1, errors=7)'])
        self.assertEqual(reuse.validate_failed_units(run,jobs,log),reuse.UNIT_RETRY)
        for bad in (log+'\nFAIL: another_test',log.replace('1108','1109'),log.replace('errors=7','errors=8'),log.replace(headers[0],'')):
            with self.assertRaises(ValueError):reuse.validate_failed_units(run,jobs,bad)
        self.assertNotIn('worker-client/chejin_worker_client/incident_evidence.py',reuse.UNIT_REPAIRS)

    def test_runner_filters_only_verified_failures_and_writes_no_false_completion(self):
        spec=importlib.util.spec_from_file_location('unit_resume_runner',ROOT/'worker-client/run_checks.py')
        runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
        with patch.dict(os.environ,{'CHEJIN_SOURCE_CHECK_RECEIPT':'fixture'}),patch.object(sys,'argv',['run_checks.py','--write-receipt','never.json']),patch.object(reuse,'load_receipt',return_value={'completed_suites':['schema','credentials'],'unittest_resume':reuse.UNIT_RETRY}),patch.object(reuse,'complete') as complete,patch.object(runner.subprocess,'run') as run:
            run.return_value.returncode=17
            self.assertEqual(runner.main(),17)
            complete.assert_not_called();self.assertEqual(run.call_count,1)
            command=run.call_args.args[0]
            self.assertEqual(command[command.index('-k'):],sum((['-k',n] for n in reuse.UNIT_RETRY),[]))
