"""Read-only access to retained legacy history; no old invitation executor."""
from fastapi import APIRouter,Request,Query
from ..security import ctx,require_permission
from ..errors import ok,Error
from .safety import redact

router=APIRouter(prefix='/api/v1/admin/invitation/legacy')
TABLES={'runs':'invitation_runs','events':'invitation_run_events','verifications':'invitation_verifications','mail-accounts':'invitation_mail_accounts','keys':'invitation_redeem_keys','proxies':'invitation_proxies'}
def public(row):
    return {key:(bool(value) if key.endswith('_ciphertext') else redact(value) if isinstance(value,str) else value) for key,value in row.items()}
@router.get('/{kind}')
def list_history(kind:str,request:Request,page:int=Query(1,ge=1),pageSize:int=Query(20,ge=1,le=100)):
    require_permission(request,'invitation.read')
    table=TABLES.get(kind)
    if not table:raise Error('NOT_FOUND','历史记录类型不存在。',404)
    db=ctx(request).db
    rows=db.all('SELECT * FROM '+table+' ORDER BY created_at DESC LIMIT ? OFFSET ?',pageSize,(page-1)*pageSize) if kind!='verifications' else db.all('SELECT * FROM '+table+' ORDER BY verified_at DESC LIMIT ? OFFSET ?',pageSize,(page-1)*pageSize)
    return ok({'items':[public(row) for row in rows],'total':db.scalar('SELECT COUNT(*) FROM '+table),'page':page,'pageSize':pageSize,'readOnly':True})
