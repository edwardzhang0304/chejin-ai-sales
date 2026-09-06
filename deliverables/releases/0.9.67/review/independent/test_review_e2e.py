"""Independent real SQLite/admin HTTP/Worker/Provider-subprocess check. Fake key only."""
import base64
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import requests
import uvicorn
from PIL import Image

from app.main import app
from app.core.database import Base, SessionLocal, engine
from app.services import auth_service
from app.models.worker import Worker
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding
from chejin_worker_client import vision_credentials as creds
from chejin_worker_client.omniauto_vision import _CancellableVisionProvider, explicit_vision_config

KEY = 'FAKE-INDEPENDENT-0967-REVIEW-KEY'

def test_sqlite_admin_worker_actual_provider_subprocess(tmp_path, monkeypatch):
    assert str(engine.url).startswith('sqlite:////private/tmp/chejin-v0967-audit.Jccyin/')
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        auth_service.create_account(db, username='independent-vision-review', display_name='Review', password='Synthetic-review-password-0967')
        db.commit()
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append((self.headers.get('x-api-key'), payload))
            result = {'vision_summary':'独立审计图片结果', 'image_ocr_text':[], 'classification':{'is_vehicle':False,'vehicle_confidence':0,'unknown':True,'non_vehicle_reason':'聊天截图'},'entities':{},'intent_hints':[],'bridge':{},'catalog_alignment':{}}
            body = json.dumps({'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}]}).encode()
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(body)
    receiver = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    rt = threading.Thread(target=receiver.serve_forever,daemon=True); rt.start()
    # Actual ASGI HTTP server on an ephemeral loopback port.
    import socket
    sock = socket.socket(); sock.bind(('127.0.0.1',0)); sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='on'))
    st = threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True); st.start()
    try:
        base = f'http://127.0.0.1:{port}'
        for _ in range(100):
            if server.started: break
            time.sleep(.05)
        assert server.started
        admin = requests.Session(); admin.headers['Origin']='http://127.0.0.1:5173'
        assert admin.post(base+'/api/auth/login',json={'username':'independent-vision-review','password':'Synthetic-review-password-0967'},timeout=5).status_code == 200
        workers=[]
        api=WorkerApiClient(base+'/api')
        for name in ('review-a','review-b'):
            response=admin.post(base+'/api/workers',json={'worker_name':name,'vision_api_key':KEY},timeout=5)
            assert response.status_code==200 and KEY not in response.text
            worker=response.json()['data']; workers.append(worker)
            api.bind(worker['id'],worker['worker_token'],name+'-instance')
            binding=Binding(worker['id'],worker['worker_token'],name+'-instance')
            assert api.get_vision_credential(binding)==KEY
            with SessionLocal() as db:
                assert KEY not in db.get(Worker,worker['id']).vision_api_key_encrypted
        creds.complete_credential_refresh(creds.begin_credential_refresh(),api.get_vision_credential(binding))
        monkeypatch.setenv('CHEJIN_BUILD_KIND','development')
        monkeypatch.setenv('CHEJIN_RPA_MODE','mock')
        monkeypatch.setenv('CUSTOMER_IMAGE_UNDERSTANDING_BASE_URL',f'http://127.0.0.1:{receiver.server_port}/v1')
        config,missing=explicit_vision_config(); assert not missing
        root=Path('/Users/zhangwentao/Documents/车金/worker-client/tests/fixtures/avatars_20260904')
        source=next(root.glob('*.png')).read_bytes()
        result=_CancellableVisionProvider(None).understand({'image':SimpleNamespace(image_bytes=source,mime_type='image/png',width=0,height=0),'config':config,'customer_text':'图片测试','message_id':'independent-review'})
        assert result['applied'] is True and result['vision_summary']=='独立审计图片结果'
        assert len(received)==1 and received[0][0]==KEY
        part=next(x for x in received[0][1]['messages'][0]['content'] if x['type']=='image')
        with Image.open(io.BytesIO(source)) as old, Image.open(io.BytesIO(base64.b64decode(part['source']['data']))) as new:
            assert old.size==new.size and old.convert('RGB').tobytes()==new.convert('RGB').tobytes()
        assert KEY not in json.dumps(result,ensure_ascii=False)
        url=base+f"/api/workers/{binding.worker_id}/vision-credential"
        assert admin.put(url,json={'vision_api_key':'   '},timeout=5).status_code==200
        assert api.get_vision_credential(binding)==KEY
        assert admin.get(base+'/api/workers',timeout=5).text.find(KEY)==-1
    finally:
        creds.clear_vision_credential()
        server.should_exit=True; st.join(5); sock.close()
        receiver.shutdown(); receiver.server_close(); rt.join(5)
