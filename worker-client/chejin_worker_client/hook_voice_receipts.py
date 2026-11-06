"""Original voice result delivery evidence shared by recovery and installation."""
from pathlib import Path
from .shared_rules import hook_contract_rules as rules


def receipt_path(root, attempt):
    keys = [attempt[k] for k in ('self_wxid', 'account_epoch', 'native_msg_id')]
    if not all(isinstance(v, str) and v not in {'', '.', '..'} and
               all(c.isalnum() or c in '-_' for c in v) for v in keys):
        raise ValueError('HOOK_VOICE_RECEIPT_IDENTITY_INVALID')
    return Path(root) / 'voice-receipts' / keys[0] / keys[1] / (keys[2] + '.json')


def acknowledged(root, attempt):
    path = receipt_path(root, attempt)
    if not path.exists():
        return False
    if path.is_symlink():
        raise ValueError('HOOK_VOICE_RECEIPT_CONFLICT')
    value = rules.loads(path.read_bytes())
    if value.get('attempt_digest') != rules.digest(attempt) or not isinstance(value.get('receipt'), dict):
        raise ValueError('HOOK_VOICE_RECEIPT_CONFLICT')
    return True


def pending_results(conn, root):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_voice_attempts'").fetchone() is None:
        return []
    return [dict(row) for row in conn.execute(
        "SELECT * FROM hook_voice_attempts WHERE state IN ('transcribed','failed')")
        if not acknowledged(root, dict(row))]


def publish(client, credentials, account, binding, attempt, root):
    from .hook_formal_c1 import _save
    # Retain the original authorization before the HTTP call. If its response
    # is lost, the server may stop advertising this voice job in its inventory.
    path = receipt_path(root, attempt)
    intent = path.with_suffix('.intent.json')
    value = {'attempt_digest': rules.digest(attempt), 'binding': binding}
    if intent.exists():
        prior = rules.loads(intent.read_bytes())
        if prior['attempt_digest'] != value['attempt_digest'] or any(
            prior['binding'][k] != binding[k] for k in ('binding_id', 'binding_revision', 'peer_wxid')):
            raise ValueError('HOOK_VOICE_RECEIPT_CONFLICT')
    else:
        _save(intent, value)
    if acknowledged(root, attempt):
        return
    receipt = client.voice_result(credentials, account, binding, attempt)
    _save(path, {'attempt_digest': rules.digest(attempt), 'receipt': receipt})


def retained_jobs(conn, root, scope, binding):
    jobs = []
    for attempt in pending_results(conn, root):
        if (attempt['self_wxid'], attempt['account_epoch'], attempt['peer_wxid']) != (
                scope.self_wxid, scope.account_epoch, binding['peer_wxid']):
            continue
        intent = receipt_path(root, attempt).with_suffix('.intent.json')
        if not intent.exists():
            continue  # Old records require the original server-advertised job.
        saved = rules.loads(intent.read_bytes())
        if saved['attempt_digest'] != rules.digest(attempt):
            raise ValueError('HOOK_VOICE_RECEIPT_CONFLICT')
        if all(saved['binding'][k] == binding[k] for k in ('binding_id', 'binding_revision', 'peer_wxid')):
            jobs.append({'native_msg_id': attempt['native_msg_id']})
    return jobs
