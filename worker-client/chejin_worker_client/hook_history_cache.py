"""Disposable history receipts; durable history/journals remain authoritative.

Bounds are on retained UTF-8 payload bytes, not a claim about process RSS.
Eviction is a cache miss: it never acknowledges, discards or repeats an action.
"""
from collections import OrderedDict
from collections.abc import MutableMapping
import json
import time

MAX_ENTRIES = 64
MAX_BYTES = 8 * 1024 * 1024
MAX_ENTRY_BYTES = 256 * 1024
TTL_SECONDS = 30


class HistoryReceiptCache(MutableMapping):
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._entries = OrderedDict()
        self.payload_bytes = 0

    def _drop(self, key):
        entry = self._entries.pop(key)
        self.payload_bytes -= len(entry[2])

    def prune(self, allowed_keys=None):
        now = self._clock()
        for key, (_, captured, _) in list(self._entries.items()):
            if now - captured >= TTL_SECONDS or (allowed_keys is not None and key not in allowed_keys):
                self._drop(key)

    def __getitem__(self, key):
        self.prune()
        barrier, captured, encoded = self._entries[key]
        self._entries.move_to_end(key)
        return barrier, captured, json.loads(encoded)

    def __setitem__(self, key, value):
        self.prune()
        if key in self._entries:
            self._drop(key)
        barrier, captured, receipt = value
        if self._clock() - captured >= TTL_SECONDS:
            return
        # Stop encoding at the per-entry budget; no second unbounded receipt
        # copy or long-lived Python object graph. The caller still uses receipt.
        encoded = bytearray()
        try:
            for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',', ':')).iterencode(receipt):
                raw = chunk.encode('utf-8')
                if len(encoded) + len(raw) > MAX_ENTRY_BYTES:
                    return
                encoded.extend(raw)
        except (TypeError, ValueError, UnicodeError):
            return  # Cache admission cannot replace a business validation result.
        while self._entries and (len(self._entries) >= MAX_ENTRIES or self.payload_bytes + len(encoded) > MAX_BYTES):
            self._drop(next(iter(self._entries)))
        self._entries[key] = (barrier, captured, bytes(encoded))
        self.payload_bytes += len(encoded)

    def __delitem__(self, key):
        self._drop(key)

    def __iter__(self):
        self.prune()
        return iter(self._entries)

    def __len__(self):
        self.prune()
        return len(self._entries)

    def clear(self):
        self._entries.clear()
        self.payload_bytes = 0
