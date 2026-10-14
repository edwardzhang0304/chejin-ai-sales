import json,sys
from sqlalchemy import create_engine
from app.services.ai_capacity_service import acquire_call_slot,release_call_slot
engine=create_engine(sys.argv[1])
slot=acquire_call_slot(engine,conversation_id=sys.argv[2],batch_id=sys.argv[3],generation_attempt=1,limit=int(sys.argv[4]))
print(json.dumps({'acquired':slot is not None,'token':slot.token if slot else None}),flush=True)
if slot:
    sys.stdin.readline()
    release_call_slot(engine,slot)
