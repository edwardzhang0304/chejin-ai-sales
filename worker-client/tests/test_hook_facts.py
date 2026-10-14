from __future__ import annotations

import json
from dataclasses import replace

import pytest

from chejin_worker_client.hook_facts import callback_observations, history_observations, ordered_text_history
from chejin_worker_client.hook_inbox import HookReceiveScope

SCOPE = HookReceiveScope("wxid_sales_test", "account-1", "process-1", 30001)
PEER = "wxid_contact_A"
MID = "6594133108973694888"


def callback(message, **envelope):
    payload = {"ServerPort": "30001", "selfwxid": SCOPE.self_wxid, "sendorrecv": "2",
        "msgnumber": "1", "msglist": [message], **envelope}
    return callback_observations(json.dumps(payload, ensure_ascii=False).encode(), SCOPE,
        event_uuid="callback-1", receive_seq=1, max_entries=10)


def incoming(**fields):
    return {"msgtype": "1", "msgsvrid": MID, "msg": "预算12.5万元\n暂时不要报价🙂",
        "time": "2026-10-06 14:39:44", "index": "1", "fromtype": "1",
        "fromid": PEER, "toid": SCOPE.self_wxid, **fields}


def history_row(**fields):
    return {"localId": "7806", "MsgSvrID": "6525284315376350549", "CreateTime": "1791543183",
        "Sequence": "1791543183000", "MsgServerSeq": "0", "MsgSequence": "772263508",
        "IsSender": "1", "Type": "1", "StrTalker": PEER, "StrContent": "HOOKTEST_ORDER_R1_S_001", **fields}


def history(rows, **scope):
    return history_observations(rows, SCOPE, event_uuid="history-page-1", receive_seq=2,
        database="MSG0.db", database_generation="database-1", max_entries=20, **scope)


def test_actual_multiline_text_unchanged_and_same_content_different_id_is_distinct():
    first = callback(incoming())[0]
    second = callback(incoming(msgsvrid="1125348102583403829"))[0]
    assert first.content == second.content == "预算12.5万元\n暂时不要报价🙂"
    assert first.native_msg_id == MID
    assert first.message_key != second.message_key
    assert first.peer_wxid == PEER and first.direction == "incoming"
    assert first.disposition == "message_candidate"
    # A process restart does not manufacture a second message identity.
    assert replace(first, process_generation="process-2").message_key == first.message_key


def test_raw_direction_is_not_message_role_and_no_id_outgoing_is_an_observation():
    received = callback(incoming(), sendorrecv="1")[0]
    assert received.direction == "incoming" and received.raw_direction == "1"
    sent = callback(incoming(msgsvrid="", fromid=SCOPE.self_wxid, toid=PEER))[0]
    assert sent.direction == "outgoing" and sent.native_msg_id is None and sent.message_key is None
    assert sent.disposition == "awaiting_send_association"
    assert sent.content == received.content


def test_actual_two_phase_voice_preserves_parent_and_does_not_guess_peer_for_supplement():
    meta = callback(incoming(msgtype="34", msg='<msg><voicemsg length="12" /></msg>', voice_hex="", voice_len="0"))[0]
    audio = b"\x02#!SILK_V3" + b"\x00\x01"
    supplement = {"index": "1", "time": "2026-10-06 14:39:45", "msgtype": "34", "msgsvrid": MID,
        "msg": "", "fromtype": "", "fromgid": "", "fromid": "", "voice_len": str(len(audio)), "voice_data": audio.hex()}
    update = callback(supplement)[0]
    assert meta.message_key == update.message_key
    assert meta.source_revision != update.source_revision
    assert meta.media_stage == "metadata_observed" and meta.content is None
    assert update.media_stage == "audio_observed" and update.audio_bytes == len(audio)
    assert update.audio_sha256 and update.peer_wxid is None and update.direction == "unknown"
    assert update.disposition == "parent_lookup_required" and update.content is None
    broken = callback({**supplement, "voice_len": "999"})[0]
    assert broken.disposition == "quarantined" and broken.reason_code == "HOOK_VOICE_AUDIO_INVALID"


@pytest.mark.parametrize("fields,reason", [
    ({"fromtype": "2", "fromgid": "test@chatroom"}, "HOOK_GROUP_EXCLUDED"),
    ({"msgtype": "49", "msg": '<appmsg type="6">file</appmsg>'}, "HOOK_MESSAGE_TYPE_UNSUPPORTED"),
    ({"fromid": "", "toid": ""}, "HOOK_PRIVATE_IDENTITY_REQUIRED"),
    ({"msgsvrid": ""}, "HOOK_MESSAGE_ID_REQUIRED"),
    ({"msgsvrid": 1.234567890123e18}, "HOOK_NATIVE_INTEGER_INVALID"),
    ({"msgsvrid": False}, "HOOK_NATIVE_INTEGER_INVALID"),
    ({"time": "sensitive invalid time"}, "HOOK_MESSAGE_TIME_INVALID"),
])
def test_unknown_group_missing_identity_and_invalid_ids_never_become_brain_text(fields, reason):
    observation = callback(incoming(**fields))[0]
    assert observation.disposition == "quarantined" and observation.content is None
    assert observation.reason_code == reason


def test_image_xml_stays_media_not_text_intent():
    observation = callback(incoming(msgtype="3", msg='<msg><img aeskey="test-secret" /></msg>', img_base64="dGVzdA=="))[0]
    assert observation.message_type == "image" and observation.content is None
    assert observation.disposition == "message_candidate"
    assert "test-secret" not in str(observation)


def test_package_shape_and_entry_bound_stop_without_partial_projection():
    with pytest.raises(ValueError, match="COUNT_INVALID"):
        callback(incoming(), msgnumber="2")
    with pytest.raises(ValueError, match="LIST_INVALID"):
        callback(incoming(), msglist=[incoming()] * 11, msgnumber="11")


def test_actual_same_second_outgoing_and_incoming_samples_order_by_scoped_msgsequence():
    a = history_row()
    b = history_row(localId="7807", MsgSvrID="1753055522999224354", Sequence="1791543183001",
        MsgSequence="772263509", StrContent="HOOKTEST_ORDER_R1_S_002")
    outgoing = history([b, a])
    assert [o.content for o in ordered_text_history(outgoing)] == [a["StrContent"], b["StrContent"]]
    assert dict(outgoing[0].ordering_fields)["MsgServerSeq"] == "0"
    a = history_row(localId="7825", MsgSvrID="724677069323593823", CreateTime="1791543697", Sequence="1791543697000",
        MsgServerSeq="1", MsgSequence="772263535", IsSender="0")
    b = {**a, "localId": "7826", "MsgSvrID": "8495113609958661530", "Sequence": "1791543697001",
        "MsgSequence": "772263536", "StrContent": "HOOKTEST_ORDER_R1_S_002"}
    ordered = ordered_text_history(history([b, a]))
    assert [o.native_msg_id for o in ordered] == [a["MsgSvrID"], b["MsgSvrID"]]
    assert ordered[0].provider_time == ordered[1].provider_time
    assert all(o.direction == "incoming" for o in ordered)


@pytest.mark.parametrize("change,reason", [
    ({"MsgSequence": "772263508"}, "EVIDENCE_CONFLICT"),
    ({"Sequence": "1791543182999"}, "EVIDENCE_CONFLICT"),
    ({"CreateTime": "1791543182"}, "EVIDENCE_CONFLICT"),
    ({"MsgSequence": "0"}, "EVIDENCE_MISSING"),
    ({"MsgSvrID": "6525284315376350549"}, "EVIDENCE_CONFLICT"),
    ({"StrTalker": "wxid_other_customer"}, "SCOPE_UNVERIFIED"),
])
def test_order_resets_conflicts_and_other_customers_hold_processing(change, reason):
    a = history_row()
    b = history_row(localId="7807", MsgSvrID="1753055522999224354", Sequence="1791543183001", MsgSequence="772263509", **{})
    b.update(change)
    with pytest.raises(ValueError, match=reason):
        ordered_text_history(history([a, b]))


def test_callback_history_share_native_identity_but_other_shards_epochs_and_voice_are_not_this_order_contract():
    candidate = callback(incoming(msgsvrid="6525284315376350549"))[0]
    row = history([history_row()])[0]
    assert candidate.message_key == row.message_key
    assert candidate.source_revision != row.source_revision
    for other in [replace(row, database="MSG1.db"), replace(row, database_generation="database-2"),
                  replace(row, account_epoch="account-2"), replace(row, message_type="voice")]:
        with pytest.raises(ValueError, match="SCOPE_UNVERIFIED"):
            ordered_text_history((row, other))
