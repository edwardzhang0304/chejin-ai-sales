"""Envelope transport only; icon proof is generated from real reference pixels."""
from copy import deepcopy

import pytest
from PIL import Image

from apps.wechat_ai_customer_service import wechat_message_envelope as envelope
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import voice_icons
from test_voice_icon_proof import sample


def observed_row():
    image, row = sample("customer")
    return voice_icons.annotate_duration_rows([row], image, [0, 0, *image.size])[0]


def wrap(rows):
    record = {"type": "voice", "content": "预算十五万", "sender_role": "customer", "ocr_items": rows}
    return envelope.apply_message_envelope_to_record(record, envelope.build_message_envelope(record))


def test_envelope_roundtrip_preserves_proof_without_mutating_source():
    row = observed_row()
    proof = deepcopy(row["_voice_visual_evidence"])
    row["_unrelated_private_metadata"] = {"should_not_escape": True}
    result = wrap(wrap([row])["ocr_items"])
    transported = result["ocr_items"][0]
    assert sidecar.voice_duration_item_like(transported)
    assert transported["_voice_visual_evidence"] == proof
    assert "_unrelated_private_metadata" not in transported
    transported["_voice_visual_evidence"]["duration_bounds"].clear()
    assert row["_voice_visual_evidence"] == proof


@pytest.mark.parametrize("change", ["missing", "uncertain", "wrong_bounds", "new_frame"])
def test_transport_does_not_create_or_upgrade_voice_proof(change):
    row = observed_row()
    if change == "missing":
        row.pop("_voice_visual_evidence")
    elif change == "uncertain":
        row["_voice_visual_evidence"]["state"] = "uncertain"
    elif change == "wrong_bounds":
        row["left"] += 1
    else:
        clean = Image.new("RGB", (400, 200), "white")
        row = voice_icons.annotate_duration_rows(wrap([row])["ocr_items"], clean, [0, 0, 400, 200])[0]
    transported = wrap([row])["ocr_items"][0]
    assert not sidecar.voice_duration_item_like(transported)


def test_transcript_exclusion_survives_summarization():
    # Transport unit: an exclusion cannot disappear and authorize a duration.
    row = observed_row()
    row["_voice_transcript_region"] = {"parent": [1, 2, 3, 4]}
    transported = wrap([row])["ocr_items"][0]
    assert transported["_voice_transcript_region"] == row["_voice_transcript_region"]
    assert not sidecar.voice_duration_item_like(transported)
