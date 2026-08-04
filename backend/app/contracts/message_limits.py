from app.contracts.c2 import c2_contract_v3


_LIMITS = c2_contract_v3().get("message_limits")
if not isinstance(_LIMITS, dict):
    raise RuntimeError("Invalid C2 message_limits contract")

C2_MESSAGE_CONTENT_MAX_CHARS = int(_LIMITS["content_max_chars"])
C2_MESSAGE_RAW_PAYLOAD_MAX_BYTES = int(
    _LIMITS["raw_payload_max_bytes"]
)
C2_MESSAGE_BATCH_MAX_ITEMS = int(_LIMITS["batch_max_items"])
C2_MESSAGE_INGEST_MAX_BYTES = int(_LIMITS["ingest_max_bytes"])
