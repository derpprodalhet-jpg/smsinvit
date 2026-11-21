from __future__ import annotations
import secrets
from uuid import UUID,uuid4
from fastapi import APIRouter,Request,Response,Body,Query
from .mapper import Mapper
from .catalog import parse_json,json_text,uuid
from .cdk import inventory_code_map,batch_no,batch_zone,BATCH_ZONE
from ..errors import Error,ok,bad,page_meta
from ..security import ctx,require_permission

router=APIRouter()
ALPHABET='ABCDEFGHJKLMNPQRSTUVWXYZ23456789abcdefghijkmnopqrstuvwxyz'
# 批次号是**导入独角时人手核对**的凭据，所以它必须与后台页面显示的日期同一天。
# `inventory_import_batches.created_at` 存的是 UTC，页面按业务时区渲染；直接在 SQL 里
# 用 DATE_FORMAT 会让 UTC 16:00–24:00 的批次早一天，导过去对不上。
BATCH_ZONE='Asia/Shanghai'
CSV_HEADER='secret'
# 保留的伪批次 id：没有 `import_batch_id` 的历史导入 / 手工创建卡密。
# 它只在批次列表与「本批卡密」只读查询里出现，不对应 `inventory_import_batches` 里的行，
# 也不提供导出与作废：拿一个假 id 去导出等于伪造批次凭据。
UNASSIGNED_BATCH_ID='unassigned'
def generate_cdk(length=20):
    if not 12<=length<=64:raise bad('CDK 长度须在 12–64。')
    return 'LC-CDK-'+''.join(secrets.choice(ALPHABET) for _ in range(length))

class Inventory:
    def __init__(self,services):self.s=services;self.db=services.db;self.mapper=Mapper(self.db,'InventoryMapper')
    def _batch_zone(self):return batch_zone(self.s.config.get('runtime.business_time_zone',BATCH_ZONE))
    def _batch_no(self,batch_id,created_at):
        """派生批次号：算法只有 `common/cdk.py::batch_no` 一处，邀请卡密页共用。"""
        return batch_no(batch_id,created_at,self.s.config.get('runtime.business_time_zone',BATCH_ZONE))
    def generate(self,sku,count,length,actor):
        if not isinstance(count,int) or isinstance(count,bool) or not 1<=count<=1000:raise bad('生成数量须在 1–1000。')
        if not isinstance(length,int) or isinstance(length,bool) or not 12<=length<=64:raise bad('CDK 长度须在 12–64。')
        with self.db.transaction():
            plan=self.mapper.lockPlan(sku)
            if not plan:raise Error('SKU_NOT_FOUND','套餐不存在。',404)
            if plan['status']!='ACTIVE':raise Error('INVENTORY_GENERATE_DENIED','套餐已停用，不能生成新卡密。',409)
            attrs=parse_json(plan['attributesJson']);kind=attrs.get('productKind')
            if not kind and attrs.get('smsService') is not None and attrs.get('smsCountry') is not None:kind='SMS_REDEEM'
            names={'SMS_REDEEM':'sms','CODEX_INVITE_REDEEM':'invitation'}
            if kind not in names:raise Error('INVENTORY_GENERATE_DENIED','套餐须选择单一的接码或邀请类型。',422)
            service=getattr(self.s,names[kind],None)
            if service is None:raise Error('SERVICE_UNAVAILABLE','兑换模块未就绪。',503)
            batch=str(uuid4());self.mapper.insertBatch(batch,sku,count,json_text({'length':length,'count':count}),json_text(attrs),actor.user_id)
            seen=set(self.mapper.listFingerprints(sku));success=duplicate=guard=0
            while success<count and guard<count*20:
                guard+=1;plaintext=generate_cdk(length);fingerprint=self.s.codec.fingerprint(plaintext)
                if fingerprint in seen:duplicate+=1;continue
                seen.add(fingerprint);id=str(uuid4());self.mapper.reserveCdk(kind,fingerprint,id);reservation=self.mapper.lockCdkReservation(kind,fingerprint)
                if not reservation or reservation['ownerInventoryId']!=id:duplicate+=1;continue
                existing=self.mapper.findCdkInventoryIds(kind,fingerprint)
                invitation=getattr(self.s,'invitation',None)
                if kind=='CODEX_INVITE_REDEEM' and invitation and hasattr(invitation,'find_inventory_ids_for_fingerprint'):
                    existing=list(dict.fromkeys(existing+invitation.find_inventory_ids_for_fingerprint(fingerprint)))
                if existing:self.mapper.recordExistingCdk(kind,fingerprint,existing[0] if len(existing)==1 else None,len(existing));duplicate+=1;continue
                self.mapper.insertItem(id,sku,self.s.codec.encrypt(plaintext),fingerprint,batch)
                if self.mapper.findItemIdByFingerprint(sku,fingerprint)!=id:self.mapper.releaseCdkReservation(kind,fingerprint,id);duplicate+=1;continue
                service.register_issued_key(id,sku,attrs,plaintext)
                self.event(id,'ISSUED',actor.user_id);success+=1
            self.mapper.finishBatch(batch,success,duplicate,0)
            self.s.audit.record(actor.user_id,'inventory.generate','sku',sku,{'requested':count,'success':success,'duplicate':duplicate,'batchId':batch})
            row=self.mapper.findBatch(batch) or {}
            return {'batchId':batch,'batchNo':self._batch_no(batch,row.get('createdAt')),'requestedCount':count,'successCount':success,'duplicateCount':duplicate,'invalidCount':0}
    def list(self,sku,page,size,status):return ok(self.mapper.listItems(sku,status or '',size,(page-1)*size),page_meta(page,size,self.mapper.countItems(sku,status or '')))
    def unassigned_count(self,sku):return int(self.mapper.countUnassignedCards(sku) or 0)
    def origin_batches(self,sku):
        """未登记卡密在邀请卡密表里的原始批次名；模块没就绪或不是邀请卡密时为空。

        历史导入的卡密没有 `inventory_import_batches` 行，唯一还留着出处的地方是
        邀请卡密表的 `batch` 列（例如 `20261007-014828` / `manual`）。查不到就如实留空，
        不拿伪批次 id 顶替批次号。
        """
        invitation=getattr(self.s,'invitation',None)
        if invitation is None or not hasattr(invitation,'unassigned_origin_batches'):return []
        return invitation.unassigned_origin_batches(sku) or []
    def _unassigned_row(self,sku,total,origins):
        """未登记批次的伪批次行：只带真实存在的字段，其余留空而不是编造。"""
        return {'id':UNASSIGNED_BATCH_ID,'skuId':sku,'batchNo':'','source':'','note':'',
                'requestedCount':None,'successCount':None,'duplicateCount':None,'invalidCount':None,
                'cardCount':total,'unassigned':True,'originBatches':origins,
                'actorEmail':'','attributesSnapshotJson':{},'createdAt':None}
    def batches(self,sku,page,size):
        rows=self.mapper.listBatches(sku,size,(page-1)*size)
        # 张数用**实时**的 inventory_items 计数：`success_count` 是生成当时的记录值，
        # 卡密被作废或删除后两者会分叉，而详情抽屉列的是实时明细。
        counts={str(row['batchId']):int(row.get('total') or 0) for row in self.mapper.countCardsByBatch(sku)}
        for row in rows:
            row['paramsJson']=parse_json(row.get('paramsJson'));row['attributesSnapshotJson']=parse_json(row.get('attributesSnapshotJson'))
            row['batchNo']=self._batch_no(row.get('id'),row.get('createdAt'))
            row['cardCount']=counts.get(str(row.get('id')),0);row['unassigned']=False
        batch_rows=int(self.mapper.countBatches(sku) or 0)
        unassigned=self.unassigned_count(sku)
        if unassigned:
            # 伪批次固定排在所有真实批次之后（第 batch_rows 行），分页时按同一下标插入，
            # 保证「已登记批次张数合计 + 未登记张数」在任意一页都能对上总数。
            index=batch_rows;offset=(page-1)*size
            if offset<=index<offset+size:rows.insert(index-offset,self._unassigned_row(sku,unassigned,self.origin_batches(sku)))
        total_cards=int(self.mapper.countItems(sku,'') or 0)
        meta=page_meta(page,size,batch_rows+(1 if unassigned else 0))
        # 页面三组数字在同一处算出来：已登记 + 未登记 = 全部（与「卡密管理」按状态合计同源）。
        meta['counts']={'registeredCards':total_cards-unassigned,'unassignedCards':unassigned,'totalCards':total_cards}
        return ok(rows,meta)
    def counts(self,sku):
        counts={'ISSUED':0,'INVALID':0};counts.update({row['status']:int(row['total']) for row in self.mapper.countByStatus(sku)})
        return {'skuId':sku,'available':counts['ISSUED'],'counts':counts}
    def event(self,id,type,actor):self.mapper.insertEvent(str(uuid4()),id,type,actor)
    def reveal(self,id,actor):
        with self.db.transaction():
            row=self.mapper.findItemForReveal(id)
            if not row:raise Error('INVENTORY_NOT_FOUND','卡密不存在。',404)
            self.event(id,'SECRET_VIEWED',actor.user_id);self.s.audit.record(actor.user_id,'inventory.secret_view','inventory',id,{})
            return {'id':id,'status':row['status'],'secret':self.s.codec.decrypt(row['secretCiphertext'])}
    def export(self,id,actor):
        with self.db.transaction():
            batch=self.mapper.findBatch(id)
            if not batch:raise Error('INVENTORY_BATCH_NOT_FOUND','发行批次不存在。',404)
            rows=self.mapper.listForBatchExport(id)
            if len(rows)>10000:raise bad('单次导出超过 10000 条，请分批处理。')
            values=[]
            for row in rows:values.append(self.s.codec.decrypt(row['secretCiphertext']));self.event(row['id'],'EXPORTED',actor.user_id)
            self.s.audit.record(actor.user_id,'inventory.batch_export','inventory_batch',id,{'count':len(values)})
            batch_no=self._batch_no(batch['id'],batch.get('createdAt'))
            # 独角「批量录入」直接粘贴 text；CSV 导入只读 secret 列，值里没有逗号、引号或
            # 换行，不需要转义。两种形态都以换行结尾且不留空行，对齐独角
            # normalizeSecrets / parseCSVSecrets 的逐行去空白语义。
            text='\n'.join(values)
            csv=CSV_HEADER+'\n'+(''.join(value+'\n' for value in values))
            return {'batchId':id,'batchNo':batch_no,'count':len(values),'secrets':values,
                    'text':text,'csv':csv,'filename':'cdk-'+batch_no+'.csv'}
    def cards(self,id,page,size,actor,reveal,sku=None):
        """批次内卡密分页；明文只在调用者具备 `inventory.export` 时出现。

        `id=UNASSIGNED_BATCH_ID` 是未登记批次的**只读**视图（没有 `import_batch_id` 的
        历史卡密）：这种卡密没有批次可依据，只能按 `sku` 圈定范围，所以必须带套餐。
        """
        with self.db.transaction():
            if id==UNASSIGNED_BATCH_ID:
                if not sku:raise bad('未登记批次必须指定套餐（skuId）。')
                rows=self.mapper.listUnassignedCards(sku,size,(page-1)*size)
                total=self.unassigned_count(sku)
            else:
                if not self.mapper.findBatch(id):raise Error('INVENTORY_BATCH_NOT_FOUND','发行批次不存在。',404)
                rows=self.mapper.listBatchCards(id,size,(page-1)*size)
                total=int(self.mapper.countBatchCards(id) or 0)
            codes=inventory_code_map(self.db,self.s.codec,[row['id'] for row in rows],reveal=reveal)
            out=[]
            for row in rows:
                detail=codes.get(str(row['id']),{})
                item={'id':row['id'],'status':row['status'],'issuedAt':row.get('issuedAt'),
                      'secretFingerprint':row.get('secretFingerprint'),'secretMasked':detail.get('codeMasked')}
                if reveal:item['secret']=detail.get('code')
                out.append(item)
            if reveal:self.s.audit.record(actor.user_id,'inventory.batch_reveal','inventory_batch',id,{'count':len(out)})
            return ok(out,page_meta(page,size,total))
    def invalidate(self,id,actor):
        with self.db.transaction():
            row=self.mapper.findItemForInvalidation(id)
            if not row:raise Error('INVENTORY_NOT_FOUND','卡密不存在。',404)
            if row['status']!='ISSUED':raise Error('INVENTORY_INVALID','仅可作废已发行的卡密。',409)
            sms=self.mapper.lockSmsKeyForInventory(id);invite=self.mapper.lockInvitationKeyForInventory(id)
            if self.mapper.invalidateItemInStatus(id,'ISSUED')!=1:raise Error('INVENTORY_INVALID','卡密状态已改变，请刷新后重试。',409)
            if sms:self.mapper.disableSmsKey(sms)
            if invite:
                self.mapper.disableInvitationKey(invite)
                self.mapper.stopQueuedInvitations(invite)
            invitation=getattr(self.s,'invitation',None)
            if invitation and hasattr(invitation,'disable_key_for_inventory'):invitation.disable_key_for_inventory(id)
            self.event(id,'INVALIDATED',actor.user_id);self.s.audit.record(actor.user_id,'inventory.invalidate','inventory',id,{})

@router.get('/api/v1/admin/inventory')
def inventory(request:Request,skuId:UUID,page:int=Query(1,ge=1),pageSize:int=Query(20,ge=1,le=100),status:str|None=Query(None,pattern='^(ISSUED|INVALID)$')):
    require_permission(request,'inventory.read');return ctx(request).inventory.list(str(skuId),page,pageSize,status)
@router.get('/api/v1/admin/inventory/count')
def counts(request:Request,skuId:UUID):require_permission(request,'inventory.read');return ok(ctx(request).inventory.counts(str(skuId)))
@router.get('/api/v1/admin/inventory/batches')
def batches(request:Request,skuId:UUID,page:int=Query(1,ge=1),pageSize:int=Query(20,ge=1,le=100)):
    require_permission(request,'inventory.read');return ctx(request).inventory.batches(str(skuId),page,pageSize)
@router.post('/api/v1/admin/inventory/generate')
def generate(request:Request,body:dict=Body(...)):return ok(ctx(request).inventory.generate(uuid(body.get('skuId')),body.get('count'),body.get('length',20),require_permission(request,'inventory.manage')))
@router.get('/api/v1/admin/inventory/batches/{id}/export')
def export(request:Request,response:Response,id:UUID):response.headers['Cache-Control']='no-store';return ok(ctx(request).inventory.export(str(id),require_permission(request,'inventory.export')))
@router.get('/api/v1/admin/inventory/batches/{id}/cards')
def batch_cards(request:Request,response:Response,id:str,page:int=Query(1,ge=1),pageSize:int=Query(20,ge=1,le=100),skuId:str|None=Query(None,max_length=36)):
    """`id` 只放行 UUID 或保留值 `unassigned`；其余输入仍是 422，不会落进 SQL。"""
    actor=require_permission(request,'inventory.read');reveal=ctx(request).permissions.allows(actor,'inventory.export')
    response.headers['Cache-Control']='no-store'
    batch=id if id==UNASSIGNED_BATCH_ID else uuid(id)
    return ctx(request).inventory.cards(batch,page,pageSize,actor,reveal,uuid(skuId) if skuId and skuId.strip() else None)
@router.get('/api/v1/admin/inventory/{id}/secret')
def reveal(request:Request,response:Response,id:UUID):response.headers['Cache-Control']='no-store';return ok(ctx(request).inventory.reveal(str(id),require_permission(request,'inventory.export')))
@router.post('/api/v1/admin/inventory/{id}/invalidate')
def invalidate(request:Request,id:UUID):ctx(request).inventory.invalidate(str(id),require_permission(request,'inventory.invalidate'));return ok(None)
