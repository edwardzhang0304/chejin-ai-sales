"""Project native observations without assigning business authority or send origin.

Raw callbacks remain in NativeInbox. Audio/image bytes and provider XML never
become text for Brain. History ordering is scoped to a verified database epoch.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .hook_inbox import HookReceiveScope, decode_callback
from .shared_rules import hook_contract_rules as rules

ORDER_FIELDS = ("localId", "CreateTime", "Sequence", "MsgServerSeq", "MsgSequence")


def _decimal(value: Any, *, optional: bool = False) -> str | None:
    if value is not None and type(value) not in (str, int):
        raise ValueError("HOOK_NATIVE_INTEGER_INVALID")
    if optional and value in (None, ""):
        return None
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", value):
        raise ValueError("HOOK_NATIVE_INTEGER_INVALID")
    return value


def _identity(value: Any) -> str | None:
    result = _decimal(value, optional=True)
    return None if result == "0" else result


def _peer(value: Any) -> str | None:
    return value if rules.wxid(value) else None


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class NativeObservation:
    event_uuid: str
    receive_seq: int
    entry_position: int
    self_wxid: str
    account_epoch: str
    process_generation: str
    source: str
    source_revision: str
    native_msg_id: str | None
    peer_wxid: str | None
    direction: str
    message_type: str
    content: str | None
    disposition: str
    reason_code: str | None
    provider_time: str | None
    raw_direction: str | None
    media_stage: str | None
    audio_sha256: str | None
    audio_bytes: int | None
    ordering_fields: tuple[tuple[str, str | None], ...]
    database: str | None = None
    database_generation: str | None = None
    from_wxid: str | None = None
    to_wxid: str | None = None
    conversation_type: str = "unknown"

    @property
    def message_key(self) -> str | None:
        # Account + native identity is stable across process restarts. Epoch
        # remains execution authority, rather than part of message identity.
        if self.native_msg_id is None:
            return None
        return "hook-msg:" + _digest([self.self_wxid, self.native_msg_id])


def _project(message: dict[str, Any], *, scope: HookReceiveScope, event_uuid: str,
             receive_seq: int, position: int, raw_direction: Any,
             history: tuple[str, str] | None = None) -> NativeObservation:
    source = "native_history" if history else "native_callback"
    mid = peer = content = media_stage = audio_hash = audio_size = provider_time = None
    direction, message_type = "unknown", "unsupported"
    disposition, reason = "quarantined", "HOOK_MESSAGE_FIELDS_INVALID"
    order: tuple[tuple[str, str | None], ...] = ()
    try:
        mid = _identity(message.get("MsgSvrID") if history else message.get("msgsvrid"))
        kind = _decimal(message.get("Type") if history else message.get("msgtype"))
        message_type = {"1": "text", "34": "voice", "3": "image"}.get(kind, "unsupported")
        if history:
            order = tuple((name, _decimal(message.get(name), optional=True)) for name in ORDER_FIELDS)
            provider_time = dict(order)["CreateTime"]
            peer = _peer(message.get("StrTalker"))
            sender = _decimal(message.get("IsSender"))
            direction = {"0": "incoming", "1": "outgoing"}.get(sender, "unknown")
            group = isinstance(message.get("StrTalker"), str) and message["StrTalker"].endswith("@chatroom")
            body = message.get("StrContent")
        else:
            from_id, to_id = message.get("fromid"), message.get("toid")
            group = str(message.get("fromtype", "")) == "2" or any(
                isinstance(message.get(field), str) and message[field].endswith("@chatroom")
                for field in ("fromid", "toid", "fromgid"))
            if to_id == scope.self_wxid and from_id != scope.self_wxid:
                peer, direction = _peer(from_id), "incoming"
            elif from_id == scope.self_wxid and to_id != scope.self_wxid:
                peer, direction = _peer(to_id), "outgoing"
            body = message.get("msg")
            provider_time = message.get("time")
            if provider_time not in (None, ""):
                if not isinstance(provider_time, str):
                    raise ValueError("HOOK_MESSAGE_TIME_INVALID")
                try:
                    datetime.strptime(provider_time, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    raise ValueError("HOOK_MESSAGE_TIME_INVALID") from None
            else:
                provider_time = None
        if group:
            raise ValueError("HOOK_GROUP_EXCLUDED")
        if message_type == "unsupported":
            raise ValueError("HOOK_MESSAGE_TYPE_UNSUPPORTED")
        if not history and message_type == "voice" and message.get("voice_data"):
            # Real audio supplements can have empty from/to. Resolve exclusively
            # through an already verified parent ID; never guess current peer.
            audio_hex = message["voice_data"]
            if not isinstance(audio_hex, str) or not re.fullmatch(r"(?:[0-9A-Fa-f]{2})+", audio_hex):
                raise ValueError("HOOK_VOICE_AUDIO_INVALID")
            audio = bytes.fromhex(audio_hex)
            length = _decimal(message.get("voice_len"))
            if length != str(len(audio)) or not audio.startswith(b"\x02#!SILK_V3"):
                raise ValueError("HOOK_VOICE_AUDIO_INVALID")
            if mid is None:
                raise ValueError("HOOK_MESSAGE_ID_REQUIRED")
            media_stage, audio_hash, audio_size = "audio_observed", hashlib.sha256(audio).hexdigest(), len(audio)
            disposition, reason = "parent_lookup_required", None
        else:
            if peer is None or direction == "unknown":
                raise ValueError("HOOK_PRIVATE_IDENTITY_REQUIRED")
            if direction == "incoming" and mid is None:
                raise ValueError("HOOK_MESSAGE_ID_REQUIRED")
            if message_type == "text":
                if not isinstance(body, str) or not body:
                    raise ValueError("HOOK_TEXT_BODY_INVALID")
                content = body
            else:
                media_stage = "metadata_observed"
            if mid is None:
                disposition, reason = "awaiting_send_association", None
            else:
                disposition, reason = "message_candidate", None
    except ValueError as exc:
        reason = str(exc)
        disposition, content = "quarantined", None
    return NativeObservation(event_uuid, receive_seq, position, scope.self_wxid,
        scope.account_epoch, scope.process_generation, source, _digest([source, message]),
        mid, peer, direction, message_type, content, disposition, reason, provider_time,
        str(raw_direction) if raw_direction is not None else None, media_stage,
        audio_hash, audio_size, order, history[0] if history else None, history[1] if history else None,
        (scope.self_wxid if direction == "outgoing" else peer) if history else (message.get('fromid') if isinstance(message.get('fromid'), str) else None),
        (scope.self_wxid if direction == "incoming" else peer) if history else (message.get('toid') if isinstance(message.get('toid'), str) else None),
        "private" if peer and direction in {"incoming", "outgoing"} and disposition != "quarantined" else "unknown")


def callback_observations(raw: bytes, scope: HookReceiveScope, *, event_uuid: str,
                          receive_seq: int, max_entries: int) -> tuple[NativeObservation, ...]:
    _validate_reference(event_uuid, receive_seq, max_entries)
    payload = decode_callback(raw, scope)
    messages = payload.get("msglist")
    if not isinstance(messages, list) or len(messages) > max_entries or any(not isinstance(m, dict) for m in messages):
        raise ValueError("HOOK_CALLBACK_MESSAGE_LIST_INVALID")
    if _decimal(payload.get("msgnumber")) != str(len(messages)):
        raise ValueError("HOOK_CALLBACK_MESSAGE_COUNT_INVALID")
    return tuple(_project(message, scope=scope, event_uuid=event_uuid, receive_seq=receive_seq,
        position=i, raw_direction=payload.get("sendorrecv")) for i, message in enumerate(messages))


def history_observations(rows: list[dict[str, Any]], scope: HookReceiveScope, *,
                         event_uuid: str, receive_seq: int, database: str,
                         database_generation: str, max_entries: int) -> tuple[NativeObservation, ...]:
    _validate_reference(event_uuid, receive_seq, max_entries)
    if (not isinstance(database, str) or not re.fullmatch(r"MSG[0-9]+\.db", database)
            or not isinstance(database_generation, str) or not database_generation.strip()):
        raise ValueError("HOOK_HISTORY_SCOPE_INVALID")
    if not isinstance(rows, list) or len(rows) > max_entries or any(not isinstance(r, dict) for r in rows):
        raise ValueError("HOOK_HISTORY_ROWS_INVALID")
    return tuple(_project(row, scope=scope, event_uuid=event_uuid, receive_seq=receive_seq,
        position=i, raw_direction=row.get("IsSender"), history=(database, database_generation))
        for i, row in enumerate(rows))


def _validate_reference(event_uuid: str, receive_seq: int, max_entries: int) -> None:
    if (not isinstance(event_uuid, str) or not event_uuid.strip() or type(receive_seq) is not int
            or receive_seq <= 0 or type(max_entries) is not int or max_entries <= 0):
        raise ValueError("HOOK_OBSERVATION_REFERENCE_INVALID")


def ordered_text_history(observations: tuple[NativeObservation, ...]) -> tuple[NativeObservation, ...]:
    """MSG0, one peer/account/database epoch, complete text candidates only.

    MsgSequence is the scoped ordering evidence verified in the October 9
    samples. Conflicts, resets and missing evidence must hold processing;
    arrival order or native ID never silently replaces it.
    """
    if not observations:
        return ()
    scopes = {(o.self_wxid, o.account_epoch, o.peer_wxid, o.database, o.database_generation) for o in observations}
    if len(scopes) != 1 or any(o.source != "native_history" or o.database != "MSG0.db"
            or not o.database_generation or o.message_type != "text" or o.disposition != "message_candidate"
            for o in observations):
        raise ValueError("HOOK_ORDER_SCOPE_UNVERIFIED")
    fields = [dict(o.ordering_fields) for o in observations]
    if any(any(not f.get(k) for k in ("localId", "CreateTime", "Sequence", "MsgSequence"))
           or any(int(f[k]) <= 0 for k in ("localId", "Sequence", "MsgSequence")) for f in fields):
        raise ValueError("HOOK_ORDER_EVIDENCE_MISSING")
    if (len({o.message_key for o in observations}) != len(observations)
            or any(len({f[k] for f in fields}) != len(fields) for k in ("localId", "Sequence", "MsgSequence"))):
        raise ValueError("HOOK_ORDER_EVIDENCE_CONFLICT")
    ordered = tuple(sorted(observations, key=lambda o: int(dict(o.ordering_fields)["MsgSequence"])))
    for a, b in zip(ordered, ordered[1:]):
        left, right = dict(a.ordering_fields), dict(b.ordering_fields)
        if any(int(left[k]) >= int(right[k]) for k in ("localId", "Sequence")) or int(left["CreateTime"]) > int(right["CreateTime"]):
            raise ValueError("HOOK_ORDER_EVIDENCE_CONFLICT")
    return ordered
