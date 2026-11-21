"""库存统计与实际分配必须用同一套筛选条件。

真实事故形态：配置的来源是 outlook，池里却混放了 cf_temp 的号（号码池本来就能
混放，补号来源与兑换来源还各配各的）。统计按当前来源过滤 → 报 0，而分配根本不看
来源 → 明明发得出号。前台再按 0 把提交整个拦掉，连"重新输入原卡密查原邮箱进度"
都挡住了。

这里跑的是真的 mail_alloc + 真的 mysql 翻译层（复用 test_invitation_account_pool_sql
的 SQLite 夹具），只有外部网络是桩。
"""
from test_invitation_account_pool_sql import pool, seed  # noqa: F401  （夹具与播种工具）

from webui import mail_alloc


def test_the_redeemable_count_does_not_depend_on_the_configured_source(pool):
    seed(pool, 'outlook-ready@example.test', status='registered', kind='outlook')
    seed(pool, 'cf-ready@example.test', status='registered', kind='cf_temp')
    # 无论当前配置的来源是什么，可兑换账号数都是池里的实数。
    assert mail_alloc.pool_available({'mail_source': 'outlook'}) == 2
    assert mail_alloc.pool_available({'mail_source': 'cf_temp'}) == 2
    assert mail_alloc.pool_available() == 2


def test_the_count_equals_what_allocation_can_actually_draw(pool):
    seed(pool, 'outlook-ready@example.test', status='registered', kind='outlook')
    seed(pool, 'cf-ready@example.test', status='registered', kind='cf_temp')
    seed(pool, 'not-registered@example.test', status='available', kind='outlook')
    available = mail_alloc.pool_available({'mail_source': 'outlook'})
    drawn = []
    while True:
        try:
            drawn.append(mail_alloc.allocate_mailbox(f'CDK-{len(drawn)}',
                                                     settings={'mail_source': 'outlook'})['email'])
        except RuntimeError:
            break
    # 统计数与"实际能发多少个"必须一致，否则前端提示会误导用户。
    assert len(drawn) == available == 2
    assert set(drawn) == {'outlook-ready@example.test', 'cf-ready@example.test'}


def test_a_pool_with_only_another_source_is_still_redeemable(pool):
    seed(pool, 'cf-only@example.test', status='registered', kind='cf_temp')
    assert mail_alloc.pool_available({'mail_source': 'outlook'}) == 1
    info = mail_alloc.allocate_mailbox('CDK-CF', settings={'mail_source': 'outlook'})
    assert info['email'] == 'cf-only@example.test' and info['kind'] == 'cf_temp'


def test_bound_and_authorized_accounts_are_not_counted(pool):
    seed(pool, 'free@example.test', status='registered', kind='outlook')
    seed(pool, 'bound@example.test', status='registered', kind='cf_temp', cdk_code='CDK-TAKEN')
    seed(pool, 'authorized@example.test', status='done', kind='outlook')
    assert mail_alloc.pool_available() == 1
