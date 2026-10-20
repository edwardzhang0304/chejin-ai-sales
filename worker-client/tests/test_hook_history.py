"""Fixed queries execute against real SQLite through a simulated DLL HTTP API."""
from dataclasses import replace
import sqlite3
import uuid

import pytest

from test_hook_gateway import lab
from chejin_worker_client import ui_lock
from chejin_worker_client.hook_history import HookHistoryCatalog, HookHistoryQuery, HISTORY_FIELDS
from chejin_worker_client.hook_gateway import HookLocalError
from chejin_worker_client.hook_outbox import HookDeliveryScope
from chejin_worker_client.hook_facts import history_observations, ordered_text_history


@pytest.fixture
def history_lab(lab):
    server,gateway,process,scope,*_=lab
    scope=replace(scope,account_epoch=str(uuid.uuid4()))
    server.catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'123'},
        {'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MediaMSG0.db','dbhandle':'456'}]
    db=sqlite3.connect(':memory:',check_same_thread=False)
    db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,'
        'CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
    db.executemany('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',[
        (1,'18446744073709551614',1,0,1791543697,1791543697000,1,10,'wxid_customer_A','同秒第一条🙂'),
        (2,'724677069323593823',1,1,1791543697,1791543697001,1,11,'wxid_customer_A','同秒第二条'),
        (3,'9001',1,0,1791543698,1791543698000,1,12,'wxid_customer_B','另一客户，不应被读取'),
        (4,'1',34,0,1791543699,1791543699000,1,13,'wxid_customer_A',None)])

    def query(body):
        assert body['dbname']=='MSG0.db'
        rows=[dict(row) for row in db.execute(body['sql']).fetchall()]
        return {'data':rows} if rows else {}

    server.query_response=query
    delivery=HookDeliveryScope(str(uuid.uuid4()),'instance-A',scope.self_wxid,scope.account_epoch,
        str(uuid.uuid4()),1,'wxid_customer_A')
    catalog=gateway.history_catalog(process,scope)
    try:yield server,gateway,process,scope,delivery,catalog,db
    finally:db.close()


def test_fixed_schema_bounds_and_pages_preserve_native_ids_and_all_types(history_lab):
    server,gateway,process,scope,delivery,catalog,_=history_lab
    database=catalog.database('MSG0.db')
    schema=HookHistoryQuery(database,'schema')
    assert gateway.history_read(process,catalog,schema)['data'][0]['name']=='MSG'
    bound=HookHistoryQuery(database,'bound',delivery)
    captured=gateway.history_read(process,catalog,bound)['data'][0]
    assert captured=={'upper_local_id':'4','row_count':'3'}
    first=HookHistoryQuery(database,'page',delivery,upper_local_id='4',page_size=2)
    rows=gateway.history_read(process,catalog,first)['data']
    assert [row['localId'] for row in rows]==['1','2']
    assert rows[0]['MsgSvrID']=='18446744073709551614'
    assert rows[0]['StrContent']=='同秒第一条🙂' and all(row['StrTalker']==delivery.peer_wxid for row in rows)
    observations=history_observations(rows,scope,event_uuid=str(uuid.uuid4()),receive_seq=1,
        database=database.name,database_generation=database.generation,max_entries=200)
    assert [o.content for o in ordered_text_history(observations)]==['同秒第一条🙂','同秒第二条']
    second=replace(first,after_local_id='2')
    tail=gateway.history_read(process,catalog,second)['data']
    assert len(tail)==1 and tail[0]['Type']=='34' and tail[0]['StrContent'] is None
    assert gateway.history_read(process,catalog,replace(first,after_local_id='4'))=={}
    count=gateway.history_read(process,catalog,HookHistoryQuery(database,'count',delivery,upper_local_id='4'))
    assert count['data']==[{'row_count':'3'}]
    assert all(x['locked'] for x in server.lock_observations) and not ui_lock.lock_summary()['locked']


@pytest.mark.parametrize('ids', [(9, 10), (98, 99, 100), (998, 999, 1000)])
@pytest.mark.parametrize('page_size', [1, 2, 100])
def test_numeric_cursor_order_across_digit_width_and_page_boundaries(history_lab, ids, page_size):
    _, gateway, process, _, delivery, catalog, db = history_lab
    db.execute('DELETE FROM MSG')
    db.executemany('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)', [
        (i, str(10000 + i), 1, 0, 1791543697, i, i, i, delivery.peer_wxid, 'synthetic')
        for i in ids
    ])
    query = HookHistoryQuery(catalog.database('MSG0.db'), 'page', delivery,
                             upper_local_id=str(ids[-1]), page_size=page_size)
    actual = []
    while query.after_local_id != query.upper_local_id:
        rows = gateway.history_read(process, catalog, query)['data']
        actual.extend(int(row['localId']) for row in rows)
        query = replace(query, after_local_id=rows[-1]['localId'])
    assert actual == list(ids)
    assert gateway.history_read(process, catalog, query) == {}


@pytest.mark.parametrize('case', ['reverse', 'duplicate', 'before_lower', 'after_upper', 'wrong_peer'])
def test_numeric_sort_does_not_relax_page_response_checks(history_lab, case):
    _, gateway, process, _, delivery, catalog, _ = history_lab
    query = HookHistoryQuery(catalog.database('MSG0.db'), 'page', delivery, upper_local_id='4')
    rows = gateway.history_read(process, catalog, query)['data']
    if case == 'reverse':
        rows.reverse()
    elif case == 'duplicate':
        rows.insert(1, dict(rows[0]))
    elif case == 'before_lower':
        rows[0]['localId'] = '0'
    elif case == 'after_upper':
        rows[-1]['localId'] = '5'
    else:
        rows[0]['StrTalker'] = 'wxid_other_customer'
    with pytest.raises(ValueError, match='HOOK_HISTORY_PAGE_(CURSOR|SCOPE)_INVALID'):
        query.validate_response({'data': rows})


@pytest.mark.parametrize('response', [{},{'data':[]},{'code':-1,'data':[]}])
def test_unknown_empty_before_upper_is_not_completion(history_lab,response):
    _,_,_,_,delivery,catalog,_=history_lab
    query=HookHistoryQuery(catalog.database('MSG0.db'),'page',delivery,upper_local_id='4',page_size=2)
    with pytest.raises(ValueError):query.validate_response(response)


@pytest.mark.parametrize('change', [dict(StrTalker='wxid_customer_B'),dict(localId='0'),
    dict(localId=1),dict(localId='5'),dict(MsgSvrID=6594133108973694888),dict(MsgSequence=1.5)])
def test_bad_or_out_of_scope_row_cannot_advance_a_page(history_lab,change):
    _,gateway,process,_,delivery,catalog,_=history_lab
    query=HookHistoryQuery(catalog.database('MSG0.db'),'page',delivery,upper_local_id='4',page_size=2)
    rows=gateway.history_read(process,catalog,query)['data']
    rows[0].update(change)
    with pytest.raises(ValueError):query.validate_response({'data':rows})


def test_catalog_change_before_read_stops_before_sql(history_lab):
    server,gateway,process,_,delivery,catalog,_=history_lab
    query=HookHistoryQuery(catalog.database('MSG0.db'),'bound',delivery)
    before=len([x for x in server.requests if x[0]=='/QueryDB'])
    server.catalog_entries[0]['dbhandle']='reopened-handle'
    with pytest.raises(HookLocalError,match='CATALOG_CHANGED'):gateway.history_read(process,catalog,query)
    assert len([x for x in server.requests if x[0]=='/QueryDB'])==before


def test_catalog_change_during_read_discards_response(history_lab):
    server,gateway,process,_,delivery,catalog,_=history_lab
    query=HookHistoryQuery(catalog.database('MSG0.db'),'bound',delivery)
    def changed(path):
        if path=='/QueryDB':server.catalog_entries[0]['dbhandle']='reopened-handle'
    server.on_request=changed
    with pytest.raises(HookLocalError,match='CATALOG_CHANGED'):gateway.history_read(process,catalog,query)


def test_new_insert_is_outside_captured_upper_and_late_insert_changes_count(history_lab):
    _,gateway,process,_,delivery,catalog,db=history_lab
    database=catalog.database('MSG0.db')
    bound=HookHistoryQuery(database,'bound',delivery)
    initial=gateway.history_read(process,catalog,bound)['data'][0]
    count=HookHistoryQuery(database,'count',delivery,upper_local_id=initial['upper_local_id'])
    db.execute('INSERT INTO MSG SELECT 5,\'9005\',Type,IsSender,CreateTime,Sequence,MsgServerSeq,MsgSequence,StrTalker,StrContent FROM MSG WHERE localId=1')
    assert gateway.history_read(process,catalog,count)['data']==[{'row_count':'3'}]
    db.execute("UPDATE MSG SET StrTalker='wxid_customer_A' WHERE localId=3")
    assert gateway.history_read(process,catalog,count)['data']==[{'row_count':'4'}]
    # The eventual durable synchronizer must compare these; a page is not a coverage certificate.


def test_query_does_not_accept_free_sql_or_another_account(history_lab):
    _,gateway,process,scope,delivery,catalog,_=history_lab
    database=catalog.database('MSG0.db')
    with pytest.raises(ValueError):HookHistoryQuery(database,'DELETE FROM MSG',delivery)
    with pytest.raises(TypeError):HookHistoryQuery(database,'page',delivery,sql='DELETE FROM MSG')
    with pytest.raises(ValueError):replace(delivery,peer_wxid="wxid_A';DELETE FROM MSG--")
    query=HookHistoryQuery(database,'bound',replace(delivery,account_epoch=str(uuid.uuid4())))
    with pytest.raises(ValueError,match='AUTHORIZATION_SCOPE'):gateway.history_read(process,catalog,query)
    bad_scope=replace(scope,self_wxid='wxid_other_sales')
    with pytest.raises(HookLocalError,match='ACCOUNT_MISMATCH'):gateway.history_catalog(process,bad_scope)


@pytest.mark.parametrize('change', ['wrong_account','duplicate_name','relative_path','missing_handle','new_shard'])
def test_catalog_scope_or_fingerprint_change_is_explicit(history_lab,change):
    server,_,_,scope,_,catalog,_=history_lab
    rows=[dict(row) for row in server.catalog_entries]
    if change=='wrong_account':rows[0]['dbpath']=rows[0]['dbpath'].replace('wxid_sales_A','wxid_other_sales')
    elif change=='duplicate_name':rows.append(dict(rows[0]))
    elif change=='relative_path':rows[0]['dbpath']='MSG0.db'
    elif change=='missing_handle':del rows[0]['dbhandle']
    else:
        rows.append({'dbpath':rows[0]['dbpath'].replace('MSG0.db','MSG1.db'),'dbhandle':'new-shard'})
        new=HookHistoryCatalog.capture(scope,{'data':rows})
        assert new.fingerprint!=catalog.fingerprint and len(new.databases)==2
        return
    with pytest.raises(ValueError):HookHistoryCatalog.capture(scope,{'data':rows})
