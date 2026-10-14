"""Pure Hook delivery rules shared by Worker and backend; no desktop imports."""
import hashlib
import json
import re
import uuid
from datetime import datetime

SCHEMA_VERSION = "hook-v1"
MAX_BYTES = 2097152
MAX_ITEMS = 200
MAX_CONTENT_CHARS = 20000
NAMESPACE = uuid.UUID("6d62451b-1575-4f44-b1df-67db1e9c97e9")
ENVELOPE_FIELDS = frozenset({"transport", "schema_version", "worker_id", "client_instance_id",
    "self_wxid", "account_epoch", "process_generation", "binding_id", "binding_revision", "peer_wxid",
    "event_uuid", "source_event_uuid", "receive_seq", "raw_evidence_digest", "observed_at", "messages"})
MESSAGE_FIELDS = frozenset({"native_msg_id", "entry_position", "direction", "message_type", "content",
    "source", "source_revision", "source_time_raw", "media_stage", "ordering_fields", "database", "database_generation",
    "from_wxid", "to_wxid", "conversation_type", "raw_direction"})
ORDER_FIELDS = frozenset({"localId", "CreateTime", "Sequence", "MsgServerSeq", "MsgSequence"})


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("HOOK_CONTRACT_DUPLICATE_FIELD")
        result[name] = value
    return result


def _constant(_):
    raise ValueError("HOOK_CONTRACT_NONFINITE_NUMBER")


def loads(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("HOOK_CONTRACT_JSON_INVALID") from None


def identifier(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def wxid(value):
    return isinstance(value, str) and re.fullmatch(r"wxid_[A-Za-z0-9_]{1,59}", value) is not None


def decimal(value, positive=False):
    return isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", value) is not None and (not positive or value != "0")


def bounded(value, maximum):
    return isinstance(value, str) and 0 < len(value) <= maximum and value.strip() == value and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def aware(value):
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.tzinfo is not None and parsed.utcoffset() is not None
    except ValueError:
        return False


def delivery_id(source_event_uuid, worker_id, instance_id, epoch, binding_id, revision):
    return str(uuid.uuid5(NAMESPACE, canonical([source_event_uuid, worker_id, instance_id, epoch, binding_id, revision])))


def validate_envelope(value):
    if not isinstance(value, dict) or set(value) != ENVELOPE_FIELDS:
        raise ValueError("HOOK_CONTRACT_FIELDS_INVALID")
    if value["transport"] != "hook" or value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("HOOK_CONTRACT_VERSION_INVALID")
    if (any(not identifier(value[k]) for k in ("worker_id", "account_epoch", "binding_id", "event_uuid", "source_event_uuid"))
            or not bounded(value["client_instance_id"], 128) or not bounded(value["process_generation"], 128)
            or not wxid(value["self_wxid"]) or not wxid(value["peer_wxid"]) or value["self_wxid"] == value["peer_wxid"]
            or type(value["binding_revision"]) is not int or value["binding_revision"] <= 0
            or not decimal(value["receive_seq"], positive=True) or not aware(value["observed_at"])
            or not isinstance(value["raw_evidence_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["raw_evidence_digest"])):
        raise ValueError("HOOK_CONTRACT_SCOPE_INVALID")
    expected = delivery_id(value["source_event_uuid"], value["worker_id"], value["client_instance_id"], value["account_epoch"], value["binding_id"], value["binding_revision"])
    if value["event_uuid"] != expected:
        raise ValueError("HOOK_CONTRACT_DELIVERY_ID_INVALID")
    messages = value["messages"]
    if not isinstance(messages, list) or not 0 < len(messages) <= MAX_ITEMS:
        raise ValueError("HOOK_CONTRACT_MESSAGES_INVALID")
    positions = set()
    for message in messages:
        if not isinstance(message, dict) or set(message) != MESSAGE_FIELDS:
            raise ValueError("HOOK_CONTRACT_MESSAGE_FIELDS_INVALID")
        if (not decimal(message["native_msg_id"], positive=True) or type(message["entry_position"]) is not int
                or not 0 <= message["entry_position"] < MAX_ITEMS or message["entry_position"] in positions
                or not isinstance(message['direction'], str) or message["direction"] not in {"incoming", "outgoing"}
                or not isinstance(message['message_type'], str) or message["message_type"] not in {"text", "voice", "image"}
                or not isinstance(message['source'], str) or message["source"] not in {"native_callback", "native_history"}
                or not isinstance(message["source_revision"], str) or not re.fullmatch(r"[0-9a-f]{64}", message["source_revision"])):
            raise ValueError("HOOK_CONTRACT_MESSAGE_IDENTITY_INVALID")
        positions.add(message["entry_position"])
        expected_from, expected_to = ((value['peer_wxid'], value['self_wxid']) if message['direction'] == 'incoming'
                                     else (value['self_wxid'], value['peer_wxid']))
        if (message['from_wxid'] != expected_from or message['to_wxid'] != expected_to
                or message['conversation_type'] != 'private'
                or (message['raw_direction'] is not None and not bounded(message['raw_direction'], 32))):
            raise ValueError('HOOK_CONTRACT_ROLE_INVALID')
        if message["source_time_raw"] is not None and not bounded(message["source_time_raw"], 64):
            raise ValueError("HOOK_CONTRACT_TIME_INVALID")
        if message["message_type"] == "text":
            if not isinstance(message["content"], str) or not 0 < len(message["content"]) <= MAX_CONTENT_CHARS or message["media_stage"] is not None:
                raise ValueError("HOOK_CONTRACT_TEXT_INVALID")
        elif message["content"] is not None or message["media_stage"] != "metadata_observed":
            raise ValueError("HOOK_CONTRACT_MEDIA_STAGE_INVALID")
        order = message["ordering_fields"]
        if not isinstance(order, dict) or set(order) - ORDER_FIELDS or any(v is not None and not decimal(v) for v in order.values()):
            raise ValueError("HOOK_CONTRACT_ORDER_INVALID")
        if message["source"] == "native_history":
            if (not isinstance(message["database"], str) or not re.fullmatch(r"MSG[0-9]+\.db", message["database"])
                    or not bounded(message["database_generation"], 128) or set(order) != ORDER_FIELDS):
                raise ValueError("HOOK_CONTRACT_HISTORY_SCOPE_INVALID")
        elif message["database"] is not None or message["database_generation"] is not None or order:
            raise ValueError("HOOK_CONTRACT_CALLBACK_SCOPE_INVALID")
    if len(canonical(value).encode("utf-8")) > MAX_BYTES:
        raise ValueError("HOOK_CONTRACT_BODY_TOO_LARGE")
    return value


def receipt(envelope):
    # Durable transport receipt only. No global coverage or AI-processing claim.
    return {"transport": "hook", "schema_version": SCHEMA_VERSION, "status": "stored",
        **{key: envelope[key] for key in ("worker_id", "client_instance_id", "self_wxid", "account_epoch", "binding_id", "binding_revision", "event_uuid", "receive_seq")},
        "payload_digest": digest(envelope), "message_count": len(envelope["messages"])}


def validate_receipt(envelope, value):
    if not isinstance(value, dict) or value != receipt(envelope):
        raise ValueError("HOOK_DELIVERY_RECEIPT_MISMATCH")
    return value
