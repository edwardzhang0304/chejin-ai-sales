import pytest
from sqlalchemy import select
from conftest import authenticated_admin_dependency
from test_hook_business_http import clean
from test_hook_account_client import identity_client
from test_hook_gateway import lab
import test_hook_formal_runtime as original
import test_c3_api as api
from app.services import c3_service
from app.core.database import SessionLocal
from app.models.c3 import ReplyAction,ReplyActionVehicleFact

@pytest.mark.parametrize('stock_size',[0,1])
def test_catalog_two_segment_formal_hook_chain(identity_client,lab,tmp_path,monkeypatch,stock_size):
    actual_claimed=original.claimed
    ids=[]
    def claimed(*args,**kwargs):
        value=actual_claimed(*args,**kwargs)
        for _ in range(stock_size):ids.append(api._create_listed_vehicle())
        return value
    monkeypatch.setattr(original,'claimed',claimed)
    class BoundaryPatch:
        def setattr(self,target,name,value):
            if target is c3_service and name=='get_ai_engine_adapter':
                factory=value
                class CatalogModel:
                    def generate_reply_decision(self,**kwargs):
                        result=factory().generate_reply_decision(**kwargs)
                        plan=result.raw_payload['omniauto_brain_result']['brain_plan']
                        plan['facts_claimed']=[{'fact_type':'inventory_absence','source_level':'product_master','source_id':'product_master'}]
                        plan['evidence_used']={'product_ids':[]}
                        return result
                value=CatalogModel
            monkeypatch.setattr(target,name,value)
    original.test_coordinator_pulls_formal_invite_binds_history_generates_and_sends(identity_client,lab,tmp_path,BoundaryPatch(),2,'nativecustomer2468')
    with SessionLocal() as db:
        actions=list(db.scalars(select(ReplyAction).order_by(ReplyAction.segment_index)))
        assert len(actions)==2
        for a in actions:
            assert a.status=='sent'
            assert a.ai_payload['vehicle_catalog_snapshot']['vehicle_ids']==sorted(ids)
            facts=list(db.scalars(select(ReplyActionVehicleFact.vehicle_id).where(ReplyActionVehicleFact.reply_action_id==a.id)))
            assert sorted(facts)==sorted(ids)
