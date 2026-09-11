import importlib.util
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[3]
spec=importlib.util.spec_from_file_location('readiness',ROOT/'backend/app/services/release_readiness.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class ReadinessTests(unittest.TestCase):
 def pending(self):return dict(task_type='add_friend',status='pending',lead_id='lead',lease_fencing_token=0)
 def test_untouched_pending_and_unbound_are_preserved(self):
  for t in [self.pending(),{**self.pending(),'status':'blocked','block_code':'SALES_WORKER_NOT_BOUND'}]:self.assertIsNone(m.task_release_blocker(t))
 def test_every_lease_field_blocks_even_terminal(self):
  for k in m.LEASE_FIELDS:
   for status in ['pending','completed']:
    with self.subTest(k=k,status=status):self.assertIsNotNone(m.task_release_blocker({**self.pending(),'status':status,k:'held'}))
 def test_execution_and_unknown_payload_stay_blocked(self):
  for k,v in [('status','running'),('status','unknown'),('task_type','chat_reply'),('task_type','future_task'),('claimed_at','time'),('current_step','claimed'),('lease_fencing_token',1),('has_execution_event',True),('has_evidence',True),('reply_action_id','reply'),('original_task_id','retry'),('lead_id',None)]:
   with self.subTest(k=k):self.assertIsNotNone(m.task_release_blocker({**self.pending(),k:v}))
 def test_settled_history_is_not_replayed(self):
  for status in m.TERMINAL:self.assertIsNone(m.task_release_blocker({'status':status,'claimed_at':'old','lease_fencing_token':8}))
