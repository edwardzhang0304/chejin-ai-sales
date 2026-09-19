"""Actual before/after incident screenshots through Worker, socket HTTP and PostgreSQL."""
from test_lead_followup_eligibility import isolated_db, http_api
from test_avatar_recognition_segmentation import segmentation_frames
from test_avatar_mask_http import _check_original_frame_continuation


def test_resegmented_reply_allows_automatic_continuation(http_api, tmp_path, monkeypatch, segmentation_frames):
    assert len(segmentation_frames['before'][0]['observations']) == 2
    assert len(segmentation_frames['after'][0]['observations']) == 4
    _check_original_frame_continuation(http_api, tmp_path, monkeypatch, segmentation_frames)
