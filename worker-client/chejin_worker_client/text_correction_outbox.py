"""Durable fact-only corrections in the existing C2 queue, without a UI Flow."""
from __future__ import annotations

import base64
import hashlib
import json
import re

from . import storage
from .models import Binding, utc_now_iso

OPERATION = "historical_text_correction"
RESULT_PREFIX = "ocr_correction_result:"
MAX_PNG_BYTES = 4 * 1024 * 1024


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _owner(binding):
    return {"worker_id": binding.worker_id, "client_instance_id": binding.client_instance_id,
            "bound_at": binding.bound_at}


def _validate_request(request, image_bytes):
    from .shared_rules import historical_text_correction
    if (request.get("operation") != OPERATION or type(request.get("version")) is not int
            or request["version"] != 1 or "image_base64" in request
            or not 0 < len(image_bytes) <= MAX_PNG_BYTES):
        raise ValueError("OCR_CORRECTION_PAYLOAD_INVALID")
    if historical_text_correction.correction_digest(request) != request.get("proof_sha256"):
        raise ValueError("OCR_CORRECTION_PROOF_CHANGED")
    if hashlib.sha256(image_bytes).hexdigest() != (request.get("proof") or {}).get("image_sha256"):
        raise ValueError("OCR_CORRECTION_IMAGE_CHANGED")
    if len(_canonical(request).encode()) > 256 * 1024:
        raise ValueError("OCR_CORRECTION_PROOF_TOO_LARGE")


def enqueue(request: dict, image_bytes: bytes, binding: Binding, *, settlement_intent: dict | None = None) -> str:
    """Freeze once. A retry reads these bytes, never re-OCRs or re-dates them."""
    _validate_request(request, image_bytes)
    identity = [request.get("message_event_id"), request.get("expected_effective_version"), request["proof_sha256"]]
    if any(not request.get(key) for key in ("conversation_id", "authorization_revision", "original_read_run_id", "message_event_id")):
        raise ValueError("OCR_CORRECTION_IDENTITY_MISSING")
    outbox_id = "c2-correction:" + hashlib.sha256(_canonical(identity).encode()).hexdigest()
    if settlement_intent:
        from apps.wechat_ai_customer_service.adapters.historical_correction_pending import validate_proof, PROPOSAL_FIELDS
        proof = validate_proof(settlement_intent["proof"])
        if (proof["conversation_id"] != request["conversation_id"]
                or any(proof["proposal"][key] != request[key] for key in PROPOSAL_FIELDS)):
            raise ValueError("OCR_CORRECTION_SETTLEMENT_CHANGED")
    payload = _canonical({"request": request, "owner": _owner(binding),
                          **({"settlement_intent": settlement_intent} if settlement_intent else {})})
    now = utc_now_iso()
    with storage.db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT operation, payload_json, correction_image FROM c2_ingest_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
        if existing:
            if (existing["operation"] != OPERATION or existing["payload_json"] != payload
                    or existing["correction_image"] != image_bytes):
                raise ValueError("OCR_CORRECTION_IDEMPOTENCY_CONFLICT")
            return outbox_id
        conn.execute("""INSERT INTO c2_ingest_outbox
            (outbox_id, operation, conversation_id, authorization_revision, read_run_id,
             payload_json, correction_image, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?)""",
            (outbox_id, OPERATION, request["conversation_id"], request["authorization_revision"],
             request["original_read_run_id"], payload, image_bytes, now, now))
        if settlement_intent:
            proof = settlement_intent["proof"]
            receipt = {"terminal_kind": "technical_failed", "conversation_id": proof["conversation_id"],
                       "error_code": "HISTORICAL_TEXT_CORRECTION_PENDING"}
            key = "inflight_finish_receipt:" + proof["flow_id"]
            prior = conn.execute("SELECT value FROM c2_runtime_state WHERE key=?", (key,)).fetchone()
            previous = json.loads(prior["value"]) if prior else {}
            if previous and previous != receipt:
                # This Flow may already have ingested the customer's question.
                # Reading succeeded; its later reply gate failed. Retain that
                # read receipt while recording the actual overall failure.
                read = previous.get("read_completion") or {}
                if not (previous.get("conversation_id") == proof["conversation_id"]
                        and previous.get("terminal_kind") == "read_confirmed"
                        and read.get("result") in {"new_facts", "no_change"}
                        and read.get("completed_at") and not previous.get("finish_request")):
                    raise ValueError("OCR_CORRECTION_FLOW_RECEIPT_CONFLICT")
                receipt = {**previous, **receipt}
            conn.execute("INSERT INTO c2_runtime_state(key,value,updated_at) VALUES(?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                         (key, _canonical(receipt), now))
        conn.commit()
    return outbox_id


def _receipt_matches(row, result):
    payload = json.loads(row["payload_json"])
    request = payload["request"]
    if (result.get("operation") != OPERATION or result.get("outbox_id") != row["outbox_id"]
            or result.get("proof_sha256") != request["proof_sha256"]
            or result.get("owner") != payload["owner"]):
        return False
    if result.get("outcome") == "rejected":
        return (row["status"] == "correction_rejected" and bool(result.get("reason"))
                and ("resolution" not in result or _closed_business_matches(payload, result)))
    return (row["status"] == "confirmed" and result.get("outcome") == "accepted"
            and bool(result.get("correction_id")) and result.get("message_event_id") == request["message_event_id"]
            and type(result.get("effective_version")) is int
            and result["effective_version"] == request["expected_effective_version"] + 1
            and result.get("effective_text_sha256") == hashlib.sha256(request["corrected_text"].encode()).hexdigest())


def _closed_business_matches(payload, result):
    from .shared_rules import historical_text_correction
    owner = payload["owner"]
    return historical_text_correction.matches_closed_business_resolution(
        result.get("resolution"), payload["request"], worker_id=owner["worker_id"],
        client_instance_id=owner["client_instance_id"])


def terminal_receipt_matches(conn, row):
    if row["status"] not in {"confirmed", "correction_rejected"}:
        return False
    saved = conn.execute("SELECT value FROM c2_runtime_state WHERE key=?", (RESULT_PREFIX + row["outbox_id"],)).fetchone()
    try:
        return bool(saved) and _receipt_matches(row, json.loads(saved["value"]))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def recovery_block_reason(binding):
    """Expose the current owner's unresolved correction, not a success label."""
    latest = {}
    with storage.db_connection() as conn:
        rows = conn.execute(f"SELECT {storage.C2_OUTBOX_METADATA_COLUMNS} FROM c2_ingest_outbox "
            "WHERE operation=? ORDER BY created_at, outbox_id", (OPERATION,)).fetchall()
        for row in rows:
            payload = json.loads(row['payload_json'])
            if payload.get('owner') == _owner(binding):
                latest[payload['request']['message_event_id']] = row
        for row in latest.values():
            if row['status'] == 'correction_rejected' and terminal_receipt_matches(conn, row):
                receipt = json.loads(conn.execute('SELECT value FROM c2_runtime_state WHERE key=?',
                    (RESULT_PREFIX + row['outbox_id'],)).fetchone()['value'])
                if not _closed_business_matches(json.loads(row['payload_json']), receipt):
                    return '原图复核未通过，旧消息已保留，需要核查故障记录。'
            if not terminal_receipt_matches(conn, row):
                return '正在核对旧消息的原图并等待后台确认，完成后可开始接单。'
    return ''


def settle(outbox_id: str, result: dict) -> None:
    """Own receipt, no read_settlement or Ledger writes; malformed ACK blocks."""
    now = utc_now_iso()
    with storage.db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        saved = conn.execute(f"SELECT {storage.C2_OUTBOX_METADATA_COLUMNS} FROM c2_ingest_outbox WHERE outbox_id=? AND operation=?", (outbox_id, OPERATION)).fetchone()
        if saved is None:
            raise ValueError("OCR_CORRECTION_OUTBOX_MISSING")
        row = dict(saved)
        payload = json.loads(row["payload_json"])
        # Persist only the bounded receipt, not arbitrary response text or bytes.
        receipt = {key: result[key] for key in ("outcome", "correction_id", "message_event_id",
                   "effective_version", "effective_text_sha256", "reason", "resolution") if key in result}
        receipt.update(operation=OPERATION, outbox_id=outbox_id,
                       proof_sha256=payload["request"]["proof_sha256"], owner=payload["owner"])
        target = "confirmed" if receipt.get("outcome") == "accepted" else "correction_rejected"
        candidate = {**row, "status": target}
        if not _receipt_matches(candidate, receipt):
            raise ValueError("OCR_CORRECTION_RECEIPT_INVALID")
        prior = conn.execute("SELECT value FROM c2_runtime_state WHERE key=?", (RESULT_PREFIX + outbox_id,)).fetchone()
        if prior:
            if json.loads(prior["value"]) != receipt or row["status"] != target:
                raise ValueError("OCR_CORRECTION_RECEIPT_CONFLICT")
            return
        if row["status"] not in {"waiting", "retry_waiting"}:
            raise ValueError("OCR_CORRECTION_STATE_CONFLICT")
        conn.execute("INSERT INTO c2_runtime_state(key,value,updated_at) VALUES (?,?,?)",
                     (RESULT_PREFIX + outbox_id, _canonical(receipt), now))
        conn.execute("UPDATE c2_ingest_outbox SET status=?,last_error=?,next_attempt_at=NULL,updated_at=? WHERE outbox_id=?",
                     (target, receipt.get("reason"), now, outbox_id))
        conn.commit()


def _retry(outbox_id, code):
    with storage.db_connection() as conn:
        row = conn.execute("SELECT attempt_count FROM c2_ingest_outbox WHERE outbox_id=? AND operation=?", (outbox_id, OPERATION)).fetchone()
        if row:
            conn.execute("""UPDATE c2_ingest_outbox SET status='retry_waiting',last_error=?,
                next_attempt_at=?,updated_at=? WHERE outbox_id=? AND status IN ('waiting','retry_waiting')""",
                (code, storage._next_attempt_iso(row["attempt_count"]), utc_now_iso(), outbox_id))
            conn.commit()


def settle_original(api, binding, item):
    """Replay the one frozen old receipt, then record confirmation durably."""
    intent = item["payload"].get("settlement_intent")
    if not intent:
        return True
    key = "ocr_correction_progress:" + item["outbox_id"]
    digest = hashlib.sha256(_canonical(intent).encode()).hexdigest()
    saved = storage.load_c2_state(key)
    if saved:
        if saved != {"settlement_sha256": digest, "settlement_confirmed": True}:
            raise ValueError("OCR_CORRECTION_SETTLEMENT_RECEIPT_CHANGED")
        return True
    api.settle_historical_text_correction_pending(binding, intent)
    storage.save_c2_state(key, {"settlement_sha256": digest, "settlement_confirmed": True})
    return True


def replay_one(api, binding: Binding, item: dict, *, finish_flow=None) -> bool:
    """True only when this operation has a durable accepted/rejected receipt."""
    from .api import ApiError
    outbox_id = item["outbox_id"]
    payload = item["payload"]
    if payload.get("owner") != _owner(binding):
        settle(outbox_id, {"outcome": "rejected", "reason": "OCR_CORRECTION_BINDING_CHANGED"})
        return True
    storage.mark_c2_outbox_attempt(outbox_id)
    try:
        settle_original(api, binding, item)
        intent = payload.get("settlement_intent")
        current_flow = storage.load_runtime_control().get("inflight_flow_id")
        if intent and current_flow == intent["proof"]["flow_id"] and finish_flow:
            finish_flow(intent["proof"])
            current_flow = storage.load_runtime_control().get("inflight_flow_id")
        if current_flow:
            return False
        with storage.db_connection() as conn:
            row = conn.execute("SELECT correction_image FROM c2_ingest_outbox WHERE outbox_id=? AND operation=?", (outbox_id, OPERATION)).fetchone()
        request = payload["request"]
        image_bytes = bytes(row["correction_image"] or b"") if row else b""
        _validate_request(request, image_bytes)
        result = api.post_wechat_message_text_correction(binding,
            {**request, "image_base64": base64.b64encode(image_bytes).decode("ascii")})
        settle(outbox_id, result)
        return True
    except ApiError as exc:
        code = exc.code if re.fullmatch(r"[A-Z0-9_]{1,100}", exc.code) else "OCR_CORRECTION_HTTP_ERROR"
        if exc.status_code in {400, 401, 403, 404, 413, 422}:
            data = exc.data if isinstance(exc.data, dict) else {}
            receipt = {"outcome": "rejected", "reason": code}
            if (exc.status_code == 422 and code == 'HISTORICAL_TEXT_CORRECTION_REJECTED'
                    and 'resolution' in data):
                receipt['resolution'] = data['resolution']
            try:
                settle(outbox_id, receipt)
            except (ValueError, TypeError, KeyError):
                _retry(outbox_id, "OCR_CORRECTION_LOCAL_PROOF_OR_RECEIPT_INVALID")
                return False
            return True
        _retry(outbox_id, code)
    except (ValueError, TypeError, KeyError):
        # A corrupted local proof or ACK is not evidence of a server rejection.
        _retry(outbox_id, "OCR_CORRECTION_LOCAL_PROOF_OR_RECEIPT_INVALID")
    except Exception as exc:
        storage.append_log("WARN", "ocr_correction_transport_failed", "历史文字纠错尚未确认，稍后重试。",
                           metadata={"outbox_id": outbox_id, "error_type": type(exc).__name__})
        _retry(outbox_id, "OCR_CORRECTION_TRANSPORT_FAILED")
    return False
