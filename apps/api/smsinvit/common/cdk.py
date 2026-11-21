"""卡密脱敏、批次号派生与按 inventory_item_id 的批量读取。

两个业务模块（接码 / Codex 邀请）后台列表统一走这里，
避免每个模块各写一份掩码规则：掩码一旦不一致，同一张卡密在不同页面显示的形态
就会不同。

批次号也在这里派生：库存台账页与邀请卡密页必须显示**同一个字符串**，否则运营
无法把「这张卡属于哪一批」对上账。两处各写一份算法正是这个问题的成因。
"""
from __future__ import annotations

from datetime import datetime,timezone
from zoneinfo import ZoneInfo

# 无卡密（或无法解密）时列表里显示的占位符。
PLACEHOLDER='—'
# 明文形态保留的前缀长度：足够区分卡密形态（如 `LC-CDK-…`），又不足以被猜测。
PREFIX_KEEP=4
# 批次号按业务时区取日期：`inventory_import_batches.created_at` 是 UTC 存储，
# 界面按业务时区渲染，直接用日期会在 UTC 16:00–24:00 之间错开一天。
BATCH_ZONE='Asia/Shanghai'


def batch_zone(name=None):
    """业务时区；名称非法时退回默认，绝不因为配置写错就 500。"""
    try:return ZoneInfo(str(name or BATCH_ZONE) or BATCH_ZONE)
    except Exception:return ZoneInfo(BATCH_ZONE)


def batch_no(batch_id,created_at,zone=None):
    """`BATCH-<业务时区 YYYYMMDD>-<id 去连字符后前 8 位大写>`。

    `created_at` 接受 datetime（naive 视为 UTC）或 ISO 字符串；拿不到就退回
    「当前业务时区的今天」，保证编号形状稳定而不是抛异常。
    """
    suffix=str(batch_id or '').replace('-','')[:8].upper()
    moment=created_at
    if isinstance(moment,str):
        try:moment=datetime.fromisoformat(moment)
        except ValueError:moment=None
    if isinstance(moment,datetime):
        moment=moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment
    else:
        moment=datetime.now(timezone.utc)
    return f'BATCH-{moment.astimezone(batch_zone(zone)):%Y%m%d}-{suffix}'


def mask_cdk(value):
    """保留前缀与后 4 位，中间用 `…`；短值退化为前 2 + … + 后 2。

    空值返回 `—`（列表里空单元格比空白更清楚）。
    """
    raw='' if value is None else str(value).strip()
    if not raw:return PLACEHOLDER
    if len(raw)<8:return f'{raw[:2]}…{raw[-2:]}'
    return f'{raw[:PREFIX_KEEP]}…{raw[-4:]}'


def inventory_code_map(db,codec,inventory_item_ids,*,reveal=False):
    """按 inventory_item_id 批量解密并脱敏。

    返回 `{inventory_item_id: {'codeMasked': str}}`；只有 `reveal=True`（调用者具备
    `inventory.export`）时才附上 `'code'` 键。`reveal=False` 时明文只存在于本函数
    栈内，用于计算掩码，绝不进入返回值。

    解密失败（密钥变更、密文损坏）按"不可显示"处理：掩码退化为 `—`，不让整个
    列表因为一条坏记录 500。
    """
    ids=[]
    for value in inventory_item_ids or ():
        item_id=str(value or '').strip()
        if item_id and item_id not in ids:ids.append(item_id)
    if not ids:return {}
    rows=db.all(
        'SELECT id,secret_ciphertext AS secretCiphertext FROM inventory_items WHERE id IN ('
        +','.join('?' for _ in ids)+')',*ids)
    out={}
    for row in rows:
        plain=None
        try:plain=codec.decrypt(row['secretCiphertext'])
        except Exception:plain=None
        detail={'codeMasked':mask_cdk(plain)}
        if reveal:detail['code']=plain
        out[str(row['id'])]=detail
    return out
