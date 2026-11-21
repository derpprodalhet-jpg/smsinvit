"""工位占位不允许泄漏：启动清空、外部实例回收、空位数统计。

背景（真机踩到的故障）：进程被杀时占位不会自己释放（Permits 没有自动回收），
三次重启泄漏了 3 个补号工位，正好等于 pool_register_concurrency，于是所有新补号
任务在门卫**无限等待**，运行记录里停在 running、日志 0 字节，从外部完全看不出原因。
"""
from smsinvit.invitation.permits import INVITE_SLOTS, POOL_SLOTS, Permits
import pytest


class FakeDb:
    """记录 SQL 与参数，只实现 Permits 用到的那几个入口。"""

    def __init__(self, occupied=(17, 18, 19), changed=0, free_slot=17):
        self.sql = []
        self.args = []
        self.occupied = tuple(occupied)
        self.changed = changed
        self.free_slot = free_slot

    def scalar(self, sql, *args):
        self.sql.append(sql)
        self.args.append(args)
        if 'COUNT(*)' in sql:
            return len(self.occupied)
        if 'owner_token IS NULL' in sql:      # 选一个空工位
            return self.free_slot
        return None

    def one(self, sql, *args):
        self.sql.append(sql)
        self.args.append(args)
        # slot 0 = 全局分配互斥；其余（邮箱互斥 / 复核保持）返回 None 表示无冲突
        return {'slot_id': 0} if 'slot_id=0' in sql else None

    def execute(self, sql, *args):
        self.sql.append(sql)
        self.args.append(args)
        changed = self.changed or (1 if 'owner_token=?' in sql else 0)
        return type('Result', (), {'rowcount': changed})()

    def transaction(self, *a, **k):
        class Tx:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        return Tx()


def test_free_slots_subtracts_occupied_slots_within_the_family():
    permits = Permits(FakeDb(occupied=(17, 18, 19)), None)
    assert permits.free_slots(POOL_SLOTS) == 13     # 16 个工位 - 占用 3
    assert permits.free_slots(INVITE_SLOTS) == 13   # 同一批占用，邀请族 16 个


def test_free_slots_never_goes_negative():
    permits = Permits(FakeDb(occupied=tuple(range(1, 40))), None)
    assert permits.free_slots(INVITE_SLOTS) == 0
    assert permits.free_slots(POOL_SLOTS) == 0


def test_free_slots_counts_only_the_family_range():
    permits = Permits(FakeDb(occupied=()), None)
    permits.free_slots(POOL_SLOTS)
    assert 'slot_id BETWEEN ? AND ?' in permits.db.sql[-1]
    assert permits.db.args[-1] == POOL_SLOTS


def test_release_all_clears_every_occupied_slot():
    db = FakeDb(changed=3)
    permits = Permits(db, None, can_reclaim=lambda: True)
    assert permits.release_all() == 3
    sql = db.sql[-1]
    assert sql.startswith('UPDATE invitation_v2_execution_permits SET owner_token=NULL')
    assert 'WHERE owner_token IS NOT NULL' in sql
    # 启动清空是无差别的：此刻已持执行器排他锁，不存在别的执行器
    assert 'instance_id<>' not in sql


def test_reclaim_foreign_only_touches_other_instances():
    db = FakeDb(changed=2)
    permits = Permits(db, None, can_reclaim=lambda: True)
    assert permits.reclaim_foreign() == 2
    assert 'instance_id<>?' in db.sql[-1] and db.args[-1] == (permits.instance_id,)


def test_reclaim_foreign_clears_nothing_without_proven_ownership():
    """拿不出"仍持执行器排他锁"的证据时，一条别人的占位都不许动。"""
    db = FakeDb(changed=2)
    permits = Permits(db, None)                                  # 没有判定 → 保守
    assert permits.reclaim_foreign() == 0
    assert Permits(db, None, can_reclaim=lambda: False).reclaim_foreign() == 0
    assert 'instance_id<>?' not in ' '.join(db.sql)


def test_release_all_refuses_when_ownership_is_denied():
    db = FakeDb(changed=3)
    permits = Permits(db, None, can_reclaim=lambda: False)
    with pytest.raises(RuntimeError):
        permits.release_all()
    assert 'WHERE owner_token IS NOT NULL' not in ' '.join(db.sql)


class FakeCodec:
    def fingerprint(self, value):
        return 'fp:' + str(value)


def test_claim_reclaims_foreign_permits_before_counting():
    """能证明自己仍持排他锁时，claim 先回收别人留下的占位。

    回收是**有条件的**：死进程泄漏的占位不清会让补号族被永久占满；但一个已经
    丢锁、还在跑的旧实例绝不能清新实例的占位（见 test_claim_never_reclaims...）。
    """
    db = FakeDb(occupied=())
    permits = Permits(db, FakeCodec(), can_reclaim=lambda: True)
    result = permits.claim('a@example.test', 'task-1', 16, POOL_SLOTS)
    assert any('instance_id<>?' in sql for sql in db.sql), db.sql
    assert result is not None and POOL_SLOTS[0] <= result[0] <= POOL_SLOTS[1]


def test_claim_never_reclaims_without_proven_ownership_but_still_claims():
    """没有所有权证据：不回收，但本身仍要能正常领工位。

    这正是"旧实例丢锁后不能清新实例占位"的防线；把它做成"拒绝领工位"会让
    正常路径一起停摆，所以只关掉回收这一处破坏性动作。
    """
    db = FakeDb(occupied=())
    permits = Permits(db, FakeCodec())
    result = permits.claim('a@example.test', 'task-1', 16, POOL_SLOTS)
    assert not any('instance_id<>?' in sql for sql in db.sql), db.sql
    assert result is not None and POOL_SLOTS[0] <= result[0] <= POOL_SLOTS[1]
    denied = Permits(FakeDb(occupied=()), FakeCodec(), can_reclaim=lambda: (_ for _ in ()).throw(ConnectionError('lost')))
    assert denied.claim('b@example.test', 'task-2', 16, POOL_SLOTS) is not None


def test_claim_refuses_when_the_family_is_full():
    permits = Permits(FakeDb(occupied=(17, 18, 19)), FakeCodec())
    # 上限 3 且已占 3 → 拿不到工位（由调用方决定跳过本轮，不再无限等）
    assert permits.claim('a@example.test', 'task-1', 3, POOL_SLOTS) is None
