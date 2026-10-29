"""Opt-in disposable PostgreSQL migration test, including data-preserving rollback.

Uses a dedicated loopback database named feiyu_migration. All data is synthetic.
This test resets that database's schemas; never point it at an application DB.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.core.database import engine
from app.core.config import get_settings
from test_feiyu_postgresql_http import Server, SECRET, payload, evidence, snapshot

pytestmark = pytest.mark.skipif(os.environ.get('FEIYU_MIGRATION_TEST') != '1', reason='explicit isolated migration opt-in required')
ROOT = Path(__file__).resolve().parents[1]


def migrate(*args, success=True):
    result = subprocess.run([sys.executable, '-m', 'alembic', *args], cwd=ROOT,
                            capture_output=True, text=True, timeout=90)
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    return {'command': ['alembic', *args], 'exit_code': result.returncode,
            'stdout': result.stdout, 'stderr': result.stderr}


def data_snapshot():
    result = {}
    inspector = inspect(engine)
    with engine.connect() as connection:
        for schema in ('public', 'wechat_ai_customer_service'):
            if schema not in inspector.get_schema_names():
                continue
            for table in inspector.get_table_names(schema=schema):
                if table == 'alembic_version':
                    continue
                rows = connection.execute(text(f'SELECT * FROM "{schema}"."{table}"')).mappings()
                result[f'{schema}.{table}'] = sorted(json.dumps(dict(row), sort_keys=True, default=str) for row in rows)
    return result


def test_fresh_upgrade_existing_data_repeat_and_receipt_preserving_rollback(monkeypatch):
    assert engine.dialect.name == 'postgresql' and engine.url.host == '127.0.0.1'
    assert engine.url.database == 'feiyu_migration'
    with engine.begin() as connection:
        connection.execute(text('DROP SCHEMA IF EXISTS wechat_ai_customer_service CASCADE'))
        connection.execute(text('DROP SCHEMA public CASCADE'))
        connection.execute(text('CREATE SCHEMA public'))
    commands = [migrate('heads'), migrate('upgrade', 'head')]
    assert commands[0]['stdout'].strip() == '20261029_0044 (head)'
    commands.append(migrate('downgrade', '20261028_0043'))  # empty table can be undone
    assert 'feiyu_lead_receipts' not in inspect(engine).get_table_names()
    settings = get_settings()
    monkeypatch.setattr(settings, 'feiyu_forward_secret', SECRET)
    monkeypatch.setattr(settings, 'feiyu_account_ids', 'account-a')
    api = Server()
    try:
        for i in (1, 2):
            r = api.post({'sales_name': f'Synthetic {i}', 'phone': f'1390000000{i}', 'enabled': True, 'sort_order': i}, '/api/sales', {})
            assert r.status_code == 200, r.text
        old = api.post(payload('manual-old', '13800000009'), '/api/leads', {})
        assert old.status_code == 200, old.text
        before = data_snapshot()
        commands.append(migrate('upgrade', 'head'))
        after = data_snapshot()
        assert after.pop('public.feiyu_lead_receipts') == []
        assert after == before  # all existing tables, not only row counts
        columns = {x['name']: x for x in inspect(engine).get_columns('feiyu_lead_receipts')}
        assert columns['payload_fingerprint']['nullable'] is False
        assert {'account_id', 'clue_id', 'lead_id', 'response_status', 'response_body'} <= columns.keys()
        assert any(x['column_names'] == ['account_id', 'clue_id'] for x in inspect(engine).get_unique_constraints('feiyu_lead_receipts'))
        assert any(x['referred_table'] == 'leads' for x in inspect(engine).get_foreign_keys('feiyu_lead_receipts'))
        commands.append(migrate('upgrade', 'head'))
        assert data_snapshot() == {**before, 'public.feiyu_lead_receipts': []}
        p = payload('after-upgrade')
        first = api.post(p); assert first.status_code == 200, first.text
        state = snapshot()
        assert len(state['leads']) == len(state['tasks']) == len(state['assignments']) == 2
        assert all(x['output']['remark_code'].startswith('CJ') for x in state['tasks'])
        persisted = data_snapshot()
        commands.append(migrate('downgrade', '20261028_0043', success=False))
        assert 'receipts must be preserved' in commands[-1]['stderr']
        assert data_snapshot() == persisted
        # Code rollback strategy: disable ingress, retain schema and receipts.
        monkeypatch.setattr(settings, 'feiyu_forward_secret', None)
        assert api.post(p).status_code == 401
        manual = api.post(payload('manual-after', '13800000008'), '/api/leads', {})
        assert manual.status_code == 200, manual.text
        monkeypatch.setattr(settings, 'feiyu_forward_secret', SECRET)
        restored = snapshot()
        assert api.post(p).content == first.content
        assert snapshot() == restored
        evidence('migration', commands=commands, old_tables_before=before,
                 old_tables_after_upgrade=after, business_after_upgrade=state,
                 business_after_ingress_restore=restored,
                 rollback='Ingress disabled, schema/receipts retained; native manual creation and replay verified. No old binary deployment simulated.')
    finally:
        api.stop(); engine.dispose()
