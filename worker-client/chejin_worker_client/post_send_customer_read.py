"""Keep the original sequence's readonly continuation in its durable send ACK."""
from .shared_rules import text_correspondence
from .storage import load_c2_state, load_runtime_control


def evidence_with_read_intent(evidence, *, claim, send_result, action_phase):
    if send_result != "sent" or action_phase != "confirmed" or not isinstance(evidence, dict):
        return evidence
    proof = text_correspondence.confirmed_post_send_customer_suffix(
        evidence, target=evidence.get("target"), text=claim.reply_text,
    )
    flow_id = str(load_runtime_control().get("inflight_flow_id") or "")
    state = load_c2_state(f"reply_sequence_flow:{flow_id}")
    if (not proof or not flow_id or not state.get("batch_id")
            or state.get("conversation_id") != claim.conversation_id
            or int(state.get("segment_count") or 0) <= 1):
        return evidence
    return {**evidence, "post_send_read_intent": {
        "version": 1, "flow_id": flow_id, "batch_id": state["batch_id"],
        "conversation_id": claim.conversation_id, "reply_action_id": claim.reply_action_id,
        "reply_text_hash": claim.reply_text_hash,
    }}


def restore_read_intent(record):
    """Before any ACK request, replay the same intent after a process restart.

    No UI occurs here. The normal sequence loop requires acknowledged receipts,
    an accepting client and a fresh backend authorization before reading.
    """
    ack = record.get("ack_payload") or {}
    intent = (ack.get("evidence") or {}).get("post_send_read_intent") or {}
    if (ack.get("send_result") != "sent" or ack.get("action_phase") != "confirmed"
            or type(intent.get("version")) is not int or intent["version"] != 1
            or intent.get("reply_action_id") != record.get("reply_action_id")
            or not intent.get("reply_text_hash")
            or intent["reply_text_hash"] != record.get("reply_text_hash")):
        return
    flow_id = str(load_runtime_control().get("inflight_flow_id") or "")
    state = load_c2_state(f"reply_sequence_flow:{flow_id}")
    if (flow_id and flow_id == intent.get("flow_id")
            and state.get("batch_id") == intent.get("batch_id")
            and state.get("conversation_id") == intent.get("conversation_id")):
        from .reply_sequence_runtime import remember_customer_interruption
        remember_customer_interruption(flow_id)
