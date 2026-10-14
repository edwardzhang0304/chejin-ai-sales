"""Shared native operation names, separate from OCR contracts and authority."""
STAGE_NAMES = frozenset({
    'hook.raw_persist', 'hook.event_registered', 'hook.history_verified', 'hook.history_read', 'hook.voice_transcription',
    'hook.send_delay', 'hook.send_call', 'hook.send_reconcile',
})
