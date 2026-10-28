"""Bounded callback metadata only: never export the message/audio body."""
import hashlib
import json
from .hook_callback_contract import MAX_CALLBACK_ENTRIES


def summarize_callback(body, *, raw_bytes, expired):
    entries, reasons = [], []
    total = None
    if body is None:
        reasons.append('callback_body_size_limit' if raw_bytes and raw_bytes > 65536 else 'callback_body_unavailable')
    else:
        try:
            raw = json.loads(body)
            messages = raw.get('msglist') if isinstance(raw, dict) else None
            if isinstance(messages, dict):
                messages = [messages]
            if not isinstance(messages, list):
                raise ValueError('invalid_callback_shape')
            total = len(messages)
            if total > MAX_CALLBACK_ENTRIES:
                reasons.append('callback_entry_limit')
            for index, message in enumerate(messages[:MAX_CALLBACK_ENTRIES]):
                if expired():
                    reasons.append('callback_time_limit')
                    break
                if not isinstance(message, dict):
                    reasons.append('callback_invalid_member')
                    continue
                entry = {k: message[k] for k in (
                    'msgtype', 'msgsvrid', 'createtime', 'timestamp', 'clientmsgid', 'localid')
                    if type(message.get(k)) in (str, int, float)}
                entry['callback_index'] = index
                text = message.get('msg')
                if isinstance(text, str):
                    data = text.encode('utf-8')
                    entry.update(message_bytes=len(data), message_sha256=hashlib.sha256(data).hexdigest(),
                                 friend_added_notice='现在可以开始聊天了' in text)
                entries.append(entry)
        except (ValueError, TypeError, RecursionError):
            reasons.append('callback_invalid_json_or_shape')
    return entries, {'original_count': total, 'collected_count': len(entries),
                     'omitted_count': None if total is None else total-len(entries),
                     'reasons': sorted(set(reasons))}
