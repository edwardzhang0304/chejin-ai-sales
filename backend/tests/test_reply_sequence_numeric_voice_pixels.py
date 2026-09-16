"""Numeric speech must reach automatic generation, sending and settlement.

Only voice duration/transcript pixels and the controlled model expectation vary. The actual
OCR, native voice entry points, Worker and HTTP/PG/SQLite flow are unchanged.
"""
import pytest

from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from reply_sequence_heartbeat import live_test_worker_heartbeat
import test_reply_sequence_media_pixels as scenario
import reply_sequence_media_desktop as desktop


@pytest.mark.parametrize('transcript,duration', [('15', '5"'), ('15', '15"'), ('007', '5"')],
                         ids=['numeric_body', 'same_value_duration_and_body', 'leading_zero_body'])
def test_numeric_voice_interrupt_continues(http_api, monkeypatch, async_generation, tmp_path, transcript, duration):
    monkeypatch.setattr(desktop, 'VOICE_DURATION_TEXT', duration)
    monkeypatch.setattr(desktop, 'TRANSCRIPT', transcript)
    monkeypatch.setattr(scenario, 'TRANSCRIPT', transcript)
    scenario.test_typing_media_interrupts_sequence_and_finishes(
        http_api, monkeypatch, async_generation, tmp_path, 'voice',
    )
