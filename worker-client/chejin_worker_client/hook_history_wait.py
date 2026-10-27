"""Read-only history holds shared by live work and retained-send recovery.

The next scheduler pass must obtain a fresh proof. A hold never certifies
history, authorizes a send, clears a journal, or changes the operator's pause.
"""

from .hook_gateway import HookLocalError


TRANSIENT_HISTORY = frozenset({
    'HOOK_HISTORY_CALLBACK_BARRIER_CHANGED', 'HOOK_HISTORY_LOCAL_PENDING',
    'HOOK_HISTORY_CALLBACK_NOT_LANDED', 'HOOK_HISTORY_BOUND_CHANGED',
    'HOOK_HISTORY_PAGINATION_INCOMPLETE', 'HOOK_HISTORY_CHANGED_DURING_READ',
    'HOOK_HISTORY_CATALOG_CHANGED', 'HOOK_HISTORY_READ_BUDGET_EXCEEDED',
    'HOOK_VOICE_PENDING',
})


def is_history_wait(error: Exception) -> bool:
    """Only classified read holds may wait; uncertain actions retain fault handling."""
    return (not getattr(error, "outcome_unknown", False)
            and (isinstance(error, HistoryCoveragePending)
                 or (isinstance(error, (ValueError, HookLocalError))
                     and str(error) in TRANSIENT_HISTORY)))


class HistoryCoveragePending(ValueError):
    """A valid coverage response is incomplete; it is not a broken journal."""


def require_retained_history(result):
    from .shared_rules import hook_history_rules
    if hook_history_rules.retained_send_history_ready(result):
        return
    reason = result.get('reason_code') or 'HOOK_HISTORY_COVERAGE_REQUIRED'
    if result.get('history_ready') is False and reason in hook_history_rules.COVERAGE_PENDING_REASONS:
        raise HistoryCoveragePending(reason)
    raise ValueError(reason)
