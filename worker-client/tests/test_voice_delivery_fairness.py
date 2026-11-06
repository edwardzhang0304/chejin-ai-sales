"""Real dispatcher/SQLite/receipt scheduling with a local HTTP result receiver."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from chejin_worker_client import storage, hook_voice_receipts as receipts
from chejin_worker_client.api import ApiError
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.shared_rules import hook_contract_rules as rules
from test_hook_voice_receipts import database, attempt


@pytest.mark.parametrize('same_customer', [False, True])
@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('advertised', [False, True])
def test_failed_result_yields_to_peers_and_recovers_original(tmp_path, monkeypatch, same_customer, restart, advertised):
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path/'worker_client.sqlite3')
    db = database(storage.DB_FILE)
    rows = [attempt(db, str(n), peer='wxid_peer'+str(1 if same_customer else n)) for n in (1,2,3)]
    db.close()
    root = tmp_path/'transactions/hook'
    bindings = [{'binding_id':'binding'+str(n), 'binding_revision':n, 'peer_wxid':'wxid_peer'+str(n),
                 'voice_jobs':[], 'retained_voice_jobs':[]} for n in ((1,) if same_customer else (1,2,3))]
    owners = {row['native_msg_id']:next(b for b in bindings if b['peer_wxid']==row['peer_wxid']) for row in rows}
    if advertised:
        for row in rows:owners[row['native_msg_id']]['voice_jobs'].append({'native_msg_id':row['native_msg_id']})
    def offline(*args):raise ApiError('HOOK_BACKEND_TEMPORARILY_UNAVAILABLE', 'synthetic offline', 503)
    for row in rows:
        with pytest.raises(ApiError):receipts.publish(SimpleNamespace(voice_result=offline), None, None, owners[row['native_msg_id']], row, root)
    calls=[]; received={}; fail_first=[True]; outside_transaction=[]
    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *args):pass
        def do_POST(self):
            value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            mid=value['attempt']['native_msg_id'];calls.append(value)
            failed=fail_first[0] and mid=='1'
            if not failed:received[mid]=value
            data=json.dumps({'voice_receipt_id':'receipt-'+mid,'native_msg_id':mid,'state':value['attempt']['state']}).encode()
            self.send_response(503 if failed else 200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Receiver);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def upload(credentials, account, binding, original):
        # A distinct writer must be available while HTTP executes.
        with storage.db_connection() as conn:
            conn.execute('BEGIN IMMEDIATE');outside_transaction.append(True);conn.rollback()
        raw=rules.canonical({'binding':binding,'attempt':original}).encode()
        try:
            with urlopen(Request(f'http://127.0.0.1:{server.server_port}/result',data=raw,headers={'Content-Type':'application/json'}),timeout=2) as r:
                return json.loads(r.read())
        except HTTPError as exc:
            exc.close();raise ApiError('HOOK_BACKEND_TEMPORARILY_UNAVAILABLE','synthetic offline',503)
    def dispatcher():
        d=HookBusinessDispatcher(SimpleNamespace(voice_result=upload),root,None,None,automatic_transcription_disabled=lambda:True)
        d.runtime=SimpleNamespace(scope=SimpleNamespace(self_wxid='wxid_owner',account_epoch='epoch',process_generation='old-generation'),credentials=None,account=None)
        return d
    d=dispatcher()
    try:
        for cycle in range(6):
            if restart and cycle==1:d=dispatcher()
            try:d._recover_voice_attempts(list(reversed(bindings)) if cycle%2 else bindings)
            except ApiError as exc:assert exc.code=='HOOK_BACKEND_TEMPORARILY_UNAVAILABLE'
        assert set(received)=={'2','3'}
        with storage.db_connection() as conn:assert receipts.pending_count(conn,root)==1
        fail_first[0]=False
        for _ in range(4):d._recover_voice_attempts(bindings)
        with storage.db_connection() as conn:
            assert receipts.pending_count(conn,root)==0
            assert [dict(r) for r in conn.execute('SELECT * FROM hook_voice_attempts ORDER BY native_msg_id')]==rows
        assert set(received)=={'1','2','3'} and len(outside_transaction)==len(calls)
        for row in rows:
            mid=row['native_msg_id'];value=received[mid]
            assert value['attempt']==row and value['binding']==owners[mid]
            proof=rules.loads(receipts.receipt_path(root,row).read_bytes())
            assert proof['attempt_digest']==rules.digest(row) and proof['receipt']['native_msg_id']==mid
        assert sum(c['attempt']['native_msg_id']=='2' for c in calls)==1
        assert sum(c['attempt']['native_msg_id']=='3' for c in calls)==1
    finally:
        server.shutdown();server.server_close();thread.join(3)
