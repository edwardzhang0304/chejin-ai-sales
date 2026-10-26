"""Read-only history holds shared by live work and retained-send recovery.

The next scheduler pass must obtain a fresh proof. A hold never certifies
history, authorizes a send, clears a journal, or changes the operator's pause.
"""

TRANSIENT_HISTORY = frozenset({
    'HOOK_HISTORY_CALLBACK_BARRIER_CHANGED', 'HOOK_HISTORY_LOCAL_PENDING',
    'HOOK_HISTORY_CALLBACK_NOT_LANDED', 'HOOK_HISTORY_BOUND_CHANGED',
    'HOOK_HISTORY_PAGINATION_INCOMPLETE', 'HOOK_HISTORY_CHANGED_DURING_READ',
    'HOOK_HISTORY_CATALOG_CHANGED', 'HOOK_HISTORY_READ_BUDGET_EXCEEDED',
    'HOOK_VOICE_PENDING',
})
