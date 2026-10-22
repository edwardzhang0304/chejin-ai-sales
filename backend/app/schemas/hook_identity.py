"""HTTP validation shares the exact native-identity rule with Worker."""
from typing import Annotated

from pydantic import BeforeValidator

from app.contracts.shared_rules import shared_adapter


def _native_identity(value):
    if not shared_adapter('hook_contract_rules').wxid(value):
        raise ValueError('HOOK_NATIVE_PRIVATE_IDENTITY_INVALID')
    return value


NativeWechatIdentity = Annotated[str, BeforeValidator(_native_identity)]
