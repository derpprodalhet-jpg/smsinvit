from __future__ import annotations
from datetime import datetime,timedelta,timezone
import re
from uuid import UUID,uuid4
from zoneinfo import ZoneInfo
from fastapi import APIRouter,Request,Body,Query
from .mapper import Mapper
from .catalog import parse_json,like
from .auth import bounded
from ..errors import Error,ok,bad,page_meta
from ..security import ctx,require_permission

router=APIRouter()
class Staff:
    def __init__(self,services):self.s=services;self.db=services.db;self.mapper=Mapper(self.db,'AdminUserMapper')
    def response(self,row):
        if not row:raise Error('USER_NOT_FOUND','用户不存在。',404)
        out=dict(row);out['roles']=parse_json(out.pop('rolesJson',None),[]);return out
    def get(self,id):return self.response(self.mapper.findDetail(str(id)))
    def valid_status(self,status):
        if status not in ('ACTIVE','DISABLED','BANNED'):raise bad('用户状态不合法。')
    def date(self,value,end):
        try:
            if len(value)==10:return (datetime.fromisoformat(value)+timedelta(days=1 if end else 0)).replace(tzinfo=ZoneInfo(self.s.config.get('runtime.business_time_zone','Asia/Shanghai'))).astimezone(timezone.utc).replace(tzinfo=None)
            return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:raise bad('日期格式错误。')
    def list(self,page,size,search,status,created_from,created_to):
        q='%'+like(search.strip())+'%' if search and search.strip() else None
        if status:self.valid_status(status)
        start=self.date(created_from,False) if created_from else None;end=self.date(created_to,True) if created_to else None
        return ok([self.response(row) for row in self.mapper.searchPage(q,status,start,end,size,(page-1)*size)],page_meta(page,size,self.mapper.searchCount(q,status,start,end)))
    def super(self,actor):
        if 'SUPER_ADMIN' not in actor.roles:raise Error('PERMISSION_DENIED','仅超级管理员可管理端账号。',403)
    def create(self,actor,body):
        self.super(actor);username=bounded(body.get('username'),30,True).strip().lower();email=bounded(body.get('email'),255,True).strip().lower();password=bounded(body.get('password'),128,True,minlen=8);display=bounded(body.get('displayName'),100) or username
        if not re.fullmatch('[a-z0-9_-]{3,30}',username):raise bad('用户名格式不合法。')
        if not re.fullmatch(r'[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}',email):raise bad('邮箱格式不合法。')
        with self.db.transaction():
            if self.mapper.existsByUsernameOrEmail(username,email):raise Error('USER_EXISTS','用户名或邮箱已存在。',409)
            id=str(uuid4());self.mapper.insertStaff(id,username,email,self.s.codec.hash_password(password),display.strip());role=self.mapper.findAdminRoleId()
            if not role:raise Error('SERVICE_UNAVAILABLE','系统角色尚未初始化。',503)
            self.mapper.insertRole(str(uuid4()),id,role,actor.user_id);self.s.audit.record(actor.user_id,'staff.create','admin_user',id,{'username':username});return self.get(id)
    def manageable(self,actor,id,allow_self=False):
        row=self.get(id)
        if self.mapper.findIdForUpdate(id) is None:raise Error('USER_NOT_FOUND','用户不存在。',404)
        if allow_self and actor.user_id==id:return
        def rank(roles):return 2 if 'SUPER_ADMIN' in roles else 1 if 'ADMIN' in roles else 0
        target_roles=row['roles'] or (('SUPER_ADMIN',) if self.mapper.isSuperAdmin(id) else ('ADMIN',))
        if rank(actor.roles)<=rank(target_roles):raise Error('PERMISSION_DENIED','只能管理比自己级别更低的管理端账号。',403)
    def status(self,actor,id,status):
        self.valid_status(status)
        with self.db.transaction():
            self.manageable(actor,id)
            if status!='ACTIVE' and actor.user_id==id:raise bad('不能停用自己的账号。')
            self.mapper.updateStatus(id,status)
            if status!='ACTIVE':self.s.auth.mapper.revokeActiveSessions(id)
            self.s.audit.record(actor.user_id,'staff.status.update','admin_user',id,{'status':status});return self.get(id)
    def reset(self,actor,id,password):
        self.super(actor);bounded(password,128,True,minlen=8)
        with self.db.transaction():self.manageable(actor,id,True);self.mapper.updatePasswordHash(id,self.s.codec.hash_password(password));self.s.auth.mapper.revokeActiveSessions(id);self.s.audit.record(actor.user_id,'staff.password.reset','admin_user',id,{});return {'reset':True}
    def delete(self,actor,id):
        self.super(actor)
        with self.db.transaction():
            self.manageable(actor,id)
            if actor.user_id==id:raise bad('不能删除自己的账号。')
            self.mapper.softDelete(id);self.s.auth.mapper.revokeActiveSessions(id);self.s.audit.record(actor.user_id,'staff.delete','admin_user',id,{});return {'deleted':True}

@router.get('/api/v1/admin/staff')
def list_staff(request:Request,page:int=Query(1,ge=1),pageSize:int=Query(20,ge=1,le=100),search:str|None=Query(None,max_length=200),status:str|None=Query(None,max_length=32),createdFrom:str|None=Query(None,max_length=64),createdTo:str|None=Query(None,max_length=64)):
    require_permission(request,'user.read');return ctx(request).staff.list(page,pageSize,search,status,createdFrom,createdTo)
@router.get('/api/v1/admin/staff/{id}')
def get_staff(request:Request,id:UUID):require_permission(request,'user.read');return ok(ctx(request).staff.get(str(id)))
@router.post('/api/v1/admin/staff')
def create_staff(request:Request,body:dict=Body(...)):return ok(ctx(request).staff.create(require_permission(request,'user.manage'),body))
@router.patch('/api/v1/admin/staff/{id}/status')
def status_staff(request:Request,id:UUID,body:dict=Body(...)):
    status=body.get('status');return ok(ctx(request).staff.status(require_permission(request,'user.manage' if status=='ACTIVE' else 'user.ban'),str(id),status))
@router.post('/api/v1/admin/staff/{id}/reset-password')
def reset_staff(request:Request,id:UUID,body:dict=Body(...)):return ok(ctx(request).staff.reset(require_permission(request,'user.manage'),str(id),body.get('newPassword')))
@router.delete('/api/v1/admin/staff/{id}')
def delete_staff(request:Request,id:UUID):return ok(ctx(request).staff.delete(require_permission(request,'user.manage'),str(id)))
