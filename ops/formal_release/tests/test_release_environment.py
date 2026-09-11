import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from maintenance import blocked_path
from build_admin import validate_api,validate_browser_receipt
class EnvironmentTests(unittest.TestCase):
 def test_grant_routes_and_encoded_paths_blocked(self):
  for path in ['/api/tasks/x/claim','/api/tasks/x/claim/','/api/workers/x/run-status','/api/workers/x/inflight-flow/start','/api/reply-actions/x/claim-send','/api/tasks/x/%63laim?x=1','/api/tasks/x/a/../claim']:
   with self.subTest(path=path):self.assertTrue(blocked_path(path))
 def test_receipts_and_login_unaffected(self):
  for path in ['/api/tasks/x/step','/api/tasks/x/lease/renew','/api/workers/x/inflight-flow/finish','/api/auth/login','/api/client-releases/latest']:
   self.assertFalse(blocked_path(path))
 def test_production_config_is_explicit(self):
  validate_api('production','/api')
  for value in ['', 'http://127.0.0.1:8000/api','https://evil.example/api']:
   with self.assertRaises(ValueError):validate_api('production',value)
 def test_fast_uat_local_is_correct(self):
  validate_api('fast-uat','http://127.0.0.1:8000/api');validate_api('fast-uat','http://192.168.1.5:8000/api')
  with self.assertRaises(ValueError):validate_api('fast-uat','https://jiangsuchejin.com/api')
 def test_browser_evidence_bound_to_artifact_and_all_user_steps(self):
  c={'environment':'production','api_base':'/api','files':{'index.html':'hash'}}
  r={'environment':'production','api_base':'/api','frontend_files':c['files'],'actual_api_base':'https://jiangsuchejin.com/api','observed_at':'now',**{k:'passed' for k in ('login','session_after_refresh','readonly_business_page','logout')}}
  self.assertTrue(validate_browser_receipt(c,r))
  for k,v in [('frontend_files',{}),('login','pending'),('session_after_refresh','failed'),('logout','pending'),('actual_api_base','http://127.0.0.1:8000/api')]:
   with self.subTest(k=k),self.assertRaises(ValueError):validate_browser_receipt(c,{**r,k:v})

class PublicationApprovalTests(unittest.TestCase):
 def test_manual_only_or_wrong_identity_cannot_publish(self):
  import json,tempfile
  from unittest.mock import patch
  import maintenance, receiver
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);snippet=root/'snippet';site=root/'site'
   snippet.write_text(maintenance.BLOCK);site.write_text(maintenance.INCLUDE)
   meta={'version':'0.9.75','commit':'a'*40,'sha256':'b'*64}
   c={'environment':'production','api_base':'/api','files':{'index.html':'hash'}}
   r={'environment':'production','api_base':'/api','frontend_files':c['files'],'actual_api_base':'https://jiangsuchejin.com/api','observed_at':'now',**{k:'passed' for k in ('login','session_after_refresh','readonly_business_page','logout')}}
   approval={**meta,'button_upgrade_accepted':True,'old_nginx_workers_drained':True,'admin_candidate':c,'admin_browser_receipt':r}
   with patch.object(maintenance,'SNIPPET',snippet),patch.object(maintenance,'SITE',site),patch.object(maintenance,'validate_ingress'):
    for k,v in [('button_upgrade_accepted',False),('sha256','wrong'),('old_nginx_workers_drained',False),('admin_browser_receipt',{**r,'login':'failed'})]:
     (root/'production-approval.json').write_text(json.dumps({**approval,k:v}))
     with self.subTest(k=k),self.assertRaises(ValueError):receiver.require_publication_approval(root,meta)
    (root/'production-approval.json').write_text(json.dumps(approval));receiver.require_publication_approval(root,meta)
    snippet.write_text('# inactive')
    with self.assertRaises(ValueError):receiver.require_publication_approval(root,meta)

class MaintenanceFailureTests(unittest.TestCase):
 def test_failed_unfreeze_restores_persistent_fence(self):
  import json,tempfile
  from unittest.mock import patch
  import maintenance
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);snippet=root/'snippet';site=root/'site'
   snippet.write_text(maintenance.BLOCK);site.write_text(maintenance.INCLUDE)
   (root/'release-approved.json').write_text(json.dumps({'backend_ready':True,'admin_browser_gate':'passed','workers_safe':True,'admin_candidate':{},'admin_browser_receipt':{}}))
   with patch.object(maintenance,'SNIPPET',snippet),patch.object(maintenance,'SITE',site),patch.object(maintenance,'validate_ingress'),patch('build_admin.validate_browser_receipt'),patch.object(maintenance,'old_workers',return_value={42}),patch.object(maintenance.time,'monotonic',side_effect=[0,61]),patch.object(maintenance.subprocess,'run') as run,patch.object(sys,'argv',['maintenance.py','disable','--evidence-dir',str(root)]):
    with self.assertRaisesRegex(RuntimeError,'freeze restored'):maintenance.main()
    self.assertEqual(snippet.read_text(),maintenance.BLOCK)
    self.assertFalse((root/'maintenance-disable.json').exists())
    self.assertEqual(sum(c.args[0]==['systemctl','reload','nginx'] for c in run.call_args_list),2)
