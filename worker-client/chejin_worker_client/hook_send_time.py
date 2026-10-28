"""Recover original local-call timing without rewriting physical send journals."""
from . import storage
from .shared_rules import hook_time_rules


def retained_call_window(intent, outcome):
    # The outcome is copied in memory only. Never patch the original file/digest.
    if outcome.get('local_clock_error') or 'local_call_window' in outcome:
        if 'local_call_window' in outcome:
            hook_time_rules.call_window(outcome['local_call_window'], intent['attempted_at'], outcome['completed_at'])
        return outcome
    if outcome.get('call_completed') is not True:
        return outcome
    with storage.db_connection() as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_active_calls'").fetchone() is None:
            return outcome
        row = conn.execute("SELECT * FROM hook_active_calls WHERE process_generation=? AND action_id=? AND kind='send_text' AND state='completed'",
            (intent['process_generation'], intent['reply_action_id'])).fetchone()
    if row is None: return outcome
    window = {'source':'retained_completed_call_v1', 'started_at':row['attempted_at'],
        'completed_at':row['completed_at'], 'elapsed_seconds':None}
    try:
        hook_time_rules.call_window(window, intent['attempted_at'], outcome['completed_at'])
    except (ValueError, TypeError):
        return outcome
    return {**outcome, 'local_call_window':window}
