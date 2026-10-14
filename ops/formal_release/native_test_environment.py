"""Assign native CI tests to explicit, disposable database/process scopes.

The test selectors remain unchanged. PostgreSQL-only tests never silently run
under SQLite, and the existing test database guards stay authoritative.
"""
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit, urlunsplit

POSTGRES_FILES = {
    'backend/tests/test_hook_gray_multiworker.py': 'hook_gray_test',
    'backend/tests/test_ai_capacity.py': 'hook_test',
    'backend/tests/test_hook_fault_recovery_http.py': 'chejin_hook_gray_recovery_test',
}


def execution_groups(groups, *, database_parent=Path('/private/tmp/chejin-hook-formal-20261011')):
    uri=os.environ.get('HOOK_TEST_POSTGRES_URL','')
    parsed=urlsplit(uri)
    if (parsed.scheme!='postgresql+psycopg' or parsed.hostname!='127.0.0.1'
            or parsed.port!=18831 or parsed.username!='postgres' or parsed.password
            or parsed.path!='/hook_test' or parsed.query or parsed.fragment):
        raise ValueError('HOOK_ISOLATED_POSTGRES_REQUIRED')
    selected={}
    for cwd,nodes in groups:
        for node in nodes:
            full=(Path(cwd)/node).as_posix()
            file=full.split('::',1)[0]
            profile=POSTGRES_FILES.get(file,'worker' if file.startswith('worker-client/') else 'backend')
            selected.setdefault(profile,[]).append(full)
    database_parent.mkdir(parents=True,exist_ok=True)
    session=Path(tempfile.mkdtemp(prefix='test-native-',dir=database_parent))
    result=[]
    for profile,nodes in selected.items():
        home=session/profile;home.mkdir()
        database=(urlunsplit(parsed._replace(path='/'+profile)) if profile in POSTGRES_FILES.values()
                  else 'sqlite:///'+str(home/'test.sqlite3'))
        result.append({'cwd':'.','nodes':nodes,'profile':profile,'env':{
            'DATABASE_URL':database,'HOOK_TEST_POSTGRES_URL':uri,
            'CHEJIN_WORKER_HOME':str(home/'worker'),'VEHICLE_IMAGE_STORAGE_ROOT':str(home/'images'),
        }})
    return result
