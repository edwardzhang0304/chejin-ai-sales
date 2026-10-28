"""Formal Hook task/binding calls retain explicit Flow and task lease headers."""
from dataclasses import dataclass
import requests
from .api import ApiError
from .hook_backend import HookWorkerCredentials, HookIdentityClient, require_backend_available
from .shared_rules import hook_contract_rules as rules

@dataclass(frozen=True)
class HookFormalClient:
    base_url: str
    timeout_seconds: float = 15
    max_response_bytes: int = 2 * 1024 * 1024

    def __post_init__(self):
        HookIdentityClient(self.base_url, self.timeout_seconds, self.max_response_bytes)

    def call(self, credentials: HookWorkerCredentials, method, path, body=None, *, task_id=None, token=None, flow_id=None):
        if method not in {'GET', 'POST'} or not path.startswith(('/tasks/', f'/workers/{credentials.worker_id}/')):
            raise ValueError('HOOK_FORMAL_ROUTE_INVALID')
        query_ok=(method=='GET' and path.endswith('?observe_only=true') and path.count('?')==1
            and '/wechat/message-batches/' in path and '&' not in path)
        if ('?' in path and not query_ok) or '#' in path or '..' in path or '://' in path:
            raise ValueError('HOOK_FORMAL_ROUTE_INVALID')
        headers = {'X-Worker-Token': credentials.worker_token, 'X-Client-Instance-Id': credentials.client_instance_id}
        if task_id is not None:
            if not rules.identifier(task_id): raise ValueError('HOOK_TASK_ID_INVALID')
            if flow_id is not None and not rules.identifier(flow_id):raise ValueError('HOOK_FLOW_ID_INVALID')
            headers['X-Inflight-Flow-Id'] = flow_id or task_id
        if token is not None:
            if type(token) is not int or token <= 0: raise ValueError('HOOK_TASK_TOKEN_INVALID')
            headers['X-Task-Lease-Fencing-Token'] = str(token)
        try:
            with requests.Session() as session:
                session.trust_env=False
                with session.request(method, self.base_url.rstrip('/')+path, headers=headers,
                        json=body, timeout=self.timeout_seconds, allow_redirects=False, stream=True) as response:
                    if 300 <= response.status_code < 400:
                        raise ApiError('HOOK_BACKEND_REDIRECT_REJECTED', '后台地址异常', response.status_code)
                    raw=bytearray()
                    for chunk in response.iter_content(8192):
                        raw.extend(chunk)
                        if len(raw)>self.max_response_bytes:
                            raise ApiError('HOOK_BACKEND_RESPONSE_TOO_LARGE','后台响应过大',502)
                    status=response.status_code
        except requests.RequestException:
            raise ApiError('HOOK_BACKEND_NETWORK_FAILED','后台请求未确认；保留原任务，不重放微信操作',0) from None
        require_backend_available(status)
        try:
            result=rules.loads(raw)
            if not isinstance(result,dict):raise ValueError()
            if status!=200 or result.get('code')!='OK':
                code=result.get('code')
                if not isinstance(code,str) or not rules.bounded(code,64):code='HOOK_FORMAL_REQUEST_REJECTED'
                raise ApiError(code,'后台任务或绑定请求未通过',status)
            if not isinstance(result.get('data'),dict):raise ValueError()
            return result['data']
        except (ValueError,TypeError,KeyError,RecursionError):
            raise ApiError('HOOK_FORMAL_RESPONSE_INVALID','后台响应格式异常',502) from None

    def prepare(self, credentials, task_id, token, account):
        return self.call(credentials,'POST',f'/tasks/{task_id}/hook-invite/prepare',account,task_id=task_id,token=token)

    def submitted(self, credentials, task_id, token, proof):
        return self.call(credentials,'POST',f'/tasks/{task_id}/hook-invite/submitted',proof,task_id=task_id,token=token)

    def bind_friend(self, credentials, task_id, account, friends):
        return self.call(credentials,'POST',f'/workers/{credentials.worker_id}/wechat/native-bindings',
            {**account,'source_task_id':task_id,'friend_list':friends,'raw_evidence_digest':rules.digest(friends)})


    def history_coverage(self, credentials, account, binding, capture_id, coverage, *, reconciliation=None):
        body={**account,'binding_id':binding['binding_id'],'binding_revision':binding['binding_revision'],
              'capture_id':capture_id,'coverage':coverage,'coverage_digest':rules.digest(coverage)}
        if reconciliation is not None:
            body['reconciliation']={**reconciliation.account(),**{k:getattr(reconciliation,k) for k in
                ('reply_action_id','task_id','flow_id','lease_fencing_token','send_token','journal_sha256')}}
        return self.call(credentials,'POST',f'/workers/{credentials.worker_id}/wechat/native-history/coverage',body)


    def native_send_claim(self,credentials,account,task_id,action_id,token,state_digest,flow_id=None):
        return self.call(credentials,'POST',f'/workers/{credentials.worker_id}/wechat/native-replies/{action_id}/claim',
            {**account,'task_id':task_id,'history_state_digest':state_digest,'clock_protocol_version':1},task_id=task_id,token=token,flow_id=flow_id)

    def native_send_begin(self,credentials,permit,payload):
        return self.call(credentials,'POST',f'/workers/{credentials.worker_id}/wechat/native-replies/{permit.reply_action_id}/begin',
            payload,task_id=permit.task_id,token=permit.lease_fencing_token,flow_id=permit.flow_id)

    def _send_receipt(self, credentials, permit, outcome, operation):
        payload = {
            **permit.account(), 'task_id': permit.task_id, 'send_token': permit.send_token,
            **{key: outcome[key] for key in ('journal_sha256', 'completed_at', 'call_completed')},
        }
        if outcome.get('local_call_window') is not None:
            payload['local_call_window'] = outcome['local_call_window']
        return self.call(
            credentials, 'POST',
            f'/workers/{credentials.worker_id}/wechat/native-replies/{permit.reply_action_id}/{operation}',
            payload, task_id=permit.task_id, token=permit.lease_fencing_token, flow_id=permit.flow_id,
        )

    def native_send_settle(self, credentials, permit, outcome):
        return self._send_receipt(credentials, permit, outcome, 'settle')


    def voice_result(self,credentials,account,binding,saved):
        body={**account,'binding_id':binding['binding_id'],'binding_revision':binding['binding_revision'],
            'peer_wxid':binding['peer_wxid'],'attempt_process_generation':saved['process_generation'],**{name:saved[name] for name in ('native_msg_id','state','audio_sha256',
            'audio_bytes','attempted_at','completed_at','response_sha256','text','error_code')},'attempt_digest':rules.digest(saved)}
        return self.call(credentials,'POST',f'/workers/{credentials.worker_id}/wechat/native-voice/result',body)


    def observe_send(self, credentials, permit, outcome):
        return self._send_receipt(credentials, permit, outcome, 'observe')
