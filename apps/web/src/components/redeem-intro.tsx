/**
 * 用户前台的服务标题行。
 *
 * 「当前可用账号：N 个」之前单独占一行、字号和正文一样，运营要求把它放到
 * 标题同一行的右侧并放大。这里做成 flex：宽屏标题在左、数量在右；窄屏自动
 * 换行到标题下面，**不会横向溢出**容器。
 *
 * 数量语义：只有后端给出 **非负整数** 才显示；收到负数/非数字（未知）就整块隐藏，
 * 绝不显示"不限量"。
 */
export function RedeemIntro({
  title,
  description,
  poolCount,
  poolUpdatedAt,
}: {
  title: string;
  description: string;
  poolCount?: number | null;
  /** 可用账号数最后一次刷新的时间；缺省则不显示，避免把一个过时快照暗示成实时。 */
  poolUpdatedAt?: Date | null;
}) {
  const showPoolCount = typeof poolCount === 'number' && Number.isFinite(poolCount) && poolCount >= 0;
  const updatedLabel =
    showPoolCount && poolUpdatedAt
      ? `更新于 ${poolUpdatedAt.toLocaleTimeString('zh-CN', { hour12: false })}`
      : null;
  return (
    <header className="redeem-intro">
      <div className="redeem-intro-main">
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {showPoolCount ? (
        <p className="redeem-availability" role="status">
          当前可用账号：<strong>{poolCount}</strong> 个
          {updatedLabel ? (
            <span className="text-xs text-[var(--color-muted)]"> （{updatedLabel}）</span>
          ) : null}
        </p>
      ) : null}
    </header>
  );
}
