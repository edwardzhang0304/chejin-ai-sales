"""Immutable Hook identity requests on a transport separate from C1's API session.

The runtime must capture credentials under its binding lock. This module does
not mutate Binding, inherit a current C1 Flow, enable work, or infer an epoch.
"""
from __future__ import annotations

import json
import re
import uuid
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import requests

from .api import ApiError
from .hook_account_issues import account_block_code, HookAccountConfirmationError
from .hook_inbox import HookReceiveScope

BACKEND_WAIT_CODES = frozenset({"HOOK_BACKEND_NETWORK_FAILED", "HOOK_BACKEND_TEMPORARILY_UNAVAILABLE"})


def require_backend_available(status: int) -> None:
    """Unavailable HTTP evidence is a wait, never permission to replay an action.

    Callers retain their original journals and reconcile through the next cycle.
    Valid HTTP responses still require their full protocol/identity validation.
    """
    if 500 <= status <= 599 or status in {408, 429}:
        raise ApiError("HOOK_BACKEND_TEMPORARILY_UNAVAILABLE", "后台暂时不可用，等待重新连接", status)


def _bounded(value: str, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(
        ord(char) < 32 or ord(char) == 127 for char in value)


def _uuid(value: str) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


@dataclass(frozen=True)
class HookWorkerCredentials:
    worker_id: str
    worker_token: str = field(repr=False)
    client_instance_id: str

    def __post_init__(self) -> None:
        if not (_uuid(self.worker_id) and _bounded(self.worker_token, 128)
                and _bounded(self.client_instance_id, 128)):
            raise ValueError("HOOK_WORKER_CREDENTIALS_INVALID")


@dataclass(frozen=True)
class HookHeartbeatIdentity:
    self_wxid: str
    process_generation: str
    deployment_revision: str
    dll_sha256: str
    account_epoch: str | None = None
    wechat_file_version: str = "3.9.10.16"

    def __post_init__(self) -> None:
        from .shared_rules import hook_contract_rules as rules
        if not (rules.wxid(self.self_wxid)
                and _bounded(self.process_generation, 128) and _bounded(self.deployment_revision, 128)
                and isinstance(self.dll_sha256, str) and re.fullmatch(r"[0-9a-fA-F]{64}", self.dll_sha256)
                and self.wechat_file_version == "3.9.10.16"
                and (self.account_epoch is None or _uuid(self.account_epoch))):
            raise ValueError("HOOK_HEARTBEAT_IDENTITY_INVALID")

    def payload(self) -> dict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class HookHeartbeatState:
    running_status: str
    rpa_component_status: str
    wechat_status: str
    current_task: str | None
    current_step: str | None
    local_lock_summary_json: str

    def __post_init__(self) -> None:
        if (self.running_status not in {"idle", "running"}
                or self.rpa_component_status not in {"ready", "unavailable"}
                or self.wechat_status not in {"logged_in", "not_found", "logged_out", "unknown"}
                or (self.current_task is not None and not _bounded(self.current_task, 255))
                or (self.current_step is not None and not _bounded(self.current_step, 64))):
            raise ValueError("HOOK_HEARTBEAT_STATUS_INVALID")
        try:
            if not isinstance(self.local_lock_summary_json, str) or len(self.local_lock_summary_json) > 16384:
                raise ValueError()
            if not isinstance(json.loads(self.local_lock_summary_json), dict):
                raise ValueError()
        except (ValueError, TypeError, RecursionError):
            raise ValueError("HOOK_HEARTBEAT_STATUS_INVALID") from None

    def payload(self) -> dict:
        return {"running_status": self.running_status, "rpa_component_status": self.rpa_component_status,
                "wechat_status": self.wechat_status, "current_task": self.current_task,
                "current_step": self.current_step, "local_lock_summary": json.loads(self.local_lock_summary_json)}


@dataclass(frozen=True)
class HookAccountReceipt:
    worker_id: str
    client_instance_id: str
    self_wxid: str | None
    account_epoch: str | None
    sales_id: str | None
    process_generation: str
    blocked: bool
    rebind_required: bool
    reason_code: str | None
    run_status: str
    server_time_utc: str | None = None
    request_started_monotonic: float | None = None
    response_received_monotonic: float | None = None
    worker_profile_json: str | None = field(default=None, repr=False)

    def worker_profile(self):
        from .models import WorkerProfile
        if self.worker_profile_json is None:
            raise ValueError("HOOK_WORKER_PROFILE_MISSING")
        return WorkerProfile.from_api(json.loads(self.worker_profile_json))

    def blocking_code(self) -> str | None:
        if self.blocked or self.rebind_required:
            return account_block_code(self.reason_code)
        if not self.self_wxid or not self.account_epoch:
            return "HOOK_ACCOUNT_NOT_CONFIRMED"
        return None

    def receive_scope(self, hook_api_port: int) -> HookReceiveScope:
        code = self.blocking_code()
        if code is not None:
            raise HookAccountConfirmationError(code)
        return HookReceiveScope(self.self_wxid, self.account_epoch, self.process_generation, hook_api_port)


@dataclass(frozen=True)
class HookIdentityClient:
    base_url: str
    timeout_seconds: float
    max_response_bytes: int

    def __post_init__(self) -> None:
        url = urlsplit(self.base_url)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                or url.query or url.fragment or any(ord(c) < 32 for c in self.base_url)
                or type(self.timeout_seconds) not in {int, float} or not 0 < self.timeout_seconds < 300
                or type(self.max_response_bytes) is not int or self.max_response_bytes <= 0):
            raise ValueError("HOOK_BACKEND_TARGET_INVALID")

    def heartbeat(self, credentials: HookWorkerCredentials, identity: HookHeartbeatIdentity,
                  state: HookHeartbeatState) -> HookAccountReceipt:
        # No shared Session, inherited Flow header, mutable lease map, retry or redirect.
        headers = {"X-Worker-Token": credentials.worker_token,
                   "X-Client-Instance-Id": credentials.client_instance_id}
        body = {**state.payload(), "client_instance_id": credentials.client_instance_id,
                "hook_context": identity.payload()}
        path = f"/workers/{credentials.worker_id}/heartbeat"
        started = time.monotonic()
        try:
            with requests.Session() as session:
                session.trust_env=False
                with session.post(self.base_url.rstrip("/") + path, headers=headers, json=body,
                        timeout=self.timeout_seconds, allow_redirects=False, stream=True) as response:
                    if 300 <= response.status_code < 400:
                        raise ApiError("HOOK_BACKEND_REDIRECT_REJECTED", "后台连接地址有误", response.status_code)
                    raw = bytearray()
                    for chunk in response.iter_content(chunk_size=8192):
                        raw.extend(chunk)
                        if len(raw) > self.max_response_bytes:
                            raise ApiError("HOOK_BACKEND_RESPONSE_TOO_LARGE", "后台响应异常", 502)
                    status = response.status_code
            received = time.monotonic()
        except requests.RequestException:
            raise ApiError("HOOK_BACKEND_NETWORK_FAILED", "后台连接失败，请检查网络", 0) from None
        # Heartbeat is idempotent reconciliation, not a physical send. A server
        # failure is unavailable evidence; it is not an identity rejection.
        require_backend_available(status)
        try:
            envelope = json.loads(raw)
            if not isinstance(envelope, dict):
                raise ValueError()
            if status != 200 or envelope.get("code") != "OK":
                # Do not expose arbitrary upstream text/body or credentials in errors.
                if status == 400 and envelope.get("code") == "WORKER_DISABLED":
                    raise ApiError("HOOK_WORKER_DISABLED", "Worker 已停用，请联系管理员", status)
                raise ApiError("HOOK_ACCOUNT_REQUEST_REJECTED", "工作微信账号核验未通过", status)
            data = envelope["data"]
            hook = data["hook_context"]
            if (data["id"] != credentials.worker_id or data["client_instance_id"] != credentials.client_instance_id
                    or type(hook["blocked"]) is not bool or type(hook["rebind_required"]) is not bool
                    or hook["process_generation"] != identity.process_generation
                    or hook["deployment_revision"] != identity.deployment_revision
                    or hook["dll_sha256"] != identity.dll_sha256
                    or hook["wechat_file_version"] != identity.wechat_file_version
                    or hook["observed_self_wxid"] != identity.self_wxid
                    or data["run_status"] not in {"running", "paused", "faulted"}):
                raise ValueError()
            if hook["rebind_required"] and not hook["blocked"]:
                raise ValueError()
            for key in ("account_epoch", "sales_id"):
                if hook[key] is not None and not _uuid(hook[key]):
                    raise ValueError()
            if not hook["blocked"] and (hook["self_wxid"] != identity.self_wxid or not hook["account_epoch"]
                    or not hook["sales_id"] or (identity.account_epoch is not None and identity.account_epoch != hook["account_epoch"])):
                raise ValueError()
            reason = hook.get("reason_code")
            if reason is not None and not _bounded(reason, 64):
                raise ValueError()
            return HookAccountReceipt(credentials.worker_id, credentials.client_instance_id,
                hook["self_wxid"], hook["account_epoch"], hook["sales_id"], identity.process_generation,
                hook["blocked"], hook["rebind_required"], reason, data["run_status"],
                data.get("last_heartbeat_at"), started, received,
                json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        except (ValueError, KeyError, TypeError, RecursionError):
            raise ApiError("HOOK_ACCOUNT_RESPONSE_INVALID", "工作微信账号核验响应异常", 502) from None

    def store_observations(self, credentials: HookWorkerCredentials, claim):
        from .shared_rules import hook_contract_rules as rules
        envelope = claim.envelope
        if envelope['worker_id'] != credentials.worker_id or envelope['client_instance_id'] != credentials.client_instance_id:
            raise ValueError("HOOK_DELIVERY_CREDENTIAL_MISMATCH")
        path = f"/workers/{credentials.worker_id}/wechat/messages/native-observations"
        try:
            with requests.Session() as session:
                session.trust_env=False
                with session.post(self.base_url.rstrip('/') + path,
                        headers={'X-Worker-Token': credentials.worker_token,
                            'X-Client-Instance-Id': credentials.client_instance_id, 'Content-Type': 'application/json'},
                        data=claim.payload_json.encode('utf-8'), timeout=self.timeout_seconds,
                        allow_redirects=False, stream=True) as response:
                    if 300 <= response.status_code < 400:
                        raise ApiError('HOOK_BACKEND_REDIRECT_REJECTED', '后台连接地址有误', response.status_code)
                    raw = bytearray()
                    for chunk in response.iter_content(chunk_size=8192):
                        raw.extend(chunk)
                        if len(raw) > self.max_response_bytes:
                            raise ApiError('HOOK_BACKEND_RESPONSE_TOO_LARGE', '后台响应异常', 502)
                    status = response.status_code
        except requests.RequestException:
            raise ApiError('HOOK_BACKEND_NETWORK_FAILED', '后台连接失败，请检查网络', 0) from None
        require_backend_available(status)
        try:
            result = rules.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            if status != 200 or result.get('code') != 'OK':
                code=result.get('code')
                if not isinstance(code,str) or not _bounded(code,128):code='HOOK_DELIVERY_REQUEST_REJECTED'
                raise ApiError(code, '消息上报未通过，原记录保留', status)
            return rules.validate_receipt(envelope, result['data'])
        except (ValueError, KeyError, TypeError, RecursionError):
            raise ApiError('HOOK_DELIVERY_RESPONSE_INVALID', '消息上报回执异常', 502) from None
