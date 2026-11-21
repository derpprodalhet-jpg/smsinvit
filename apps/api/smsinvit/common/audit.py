from __future__ import annotations
from contextvars import ContextVar
from uuid import UUID,uuid4
from fastapi import APIRouter,Request,Query
from .mapper import Mapper
from .catalog import json_text,parse_json,like
from ..errors import ok,page_meta
from ..security import ctx,require_permission

request_id_context=ContextVar('smsinvit_request_id',default=None)
router=APIRouter()
class Audit:
    def __init__(self,db):self.db=db;self.mapper=Mapper(db,'AuditLogMapper')
    def record(self,actor,action,resource_type,resource_id,safe_facts=None):
        # Facts supplied by service code, never serialize a request or arbitrary
        # database record. All audit writes join the business transaction.
        with self.db.transaction():self.mapper.insert(str(uuid4()),str(actor) if actor else None,action,resource_type,str(resource_id) if resource_id is not None else None,json_text(safe_facts or {}),request_id_context.get() or str(uuid4()))
    def list(self,page,size,action,type,actor,id):
        q='%'+like(action.strip())+'%' if action and action.strip() else None
        rows=self.mapper.page(q,type or None,str(actor) if actor else None,id or None,size,(page-1)*size)
        for row in rows:
            if 'safeFactsJson' in row:row['safeFactsJson']=parse_json(row['safeFactsJson'])
            if 'metadataJson' in row:row['metadataJson']=parse_json(row['metadataJson'])
        return ok(rows,page_meta(page,size,self.mapper.count(q,type or None,str(actor) if actor else None,id or None)))

@router.get('/api/v1/admin/audit-logs')
def list_audit(request:Request,page:int=Query(1,ge=1),pageSize:int=Query(20,ge=1,le=100),action:str|None=Query(None,max_length=200),resourceType:str|None=Query(None,max_length=64),actorUserId:UUID|None=None,resourceId:str|None=Query(None,max_length=100)):
    # 专用权限点（库内早已定义），不再复用 settings.read。
    require_permission(request,'audit.read');return ctx(request).audit.list(page,pageSize,action,resourceType,actorUserId,resourceId)
