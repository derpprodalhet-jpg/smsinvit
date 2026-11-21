# 2026-11-21 · 下线 GPT 直充与 X 模块、项目更名为 SmsInvit

变更来源：把上游交付目录 `SOURCE_DIR` 全量复制进本仓库后，删掉「GPT 直充」和「X Premium」
两条业务，只保留「接码（SMS）」与「Codex 邀请（含账号工厂注册）」，并把项目更名为 SmsInvit。
`SOURCE_DIR` 未做任何修改（下文所有"原项目/基线"均指该目录）。仓库内已无旧项目名的任何字样，
命名替换口径见第 4 节。

## 1. 删除的业务面

| 区域 | 删除内容 |
| --- | --- |
| 后端 | `apps/api/smsinvit/gptdirect/`（17 个文件）、`common/gpt_plans.py`、`scripts/seed_gpt_plans.py` |
| 管理端 | `apps/admin/src/pages/gpt-direct/**`（6 页）、`lib/gpt-direct-labels.ts`、`components/gpt-card-tools.tsx`、`components/gpt-step-timeline.tsx` |
| 用户端 | `apps/web/src/components/gpt-direct-redeem-panel.tsx`、`lib/gpt-direct.ts`、`public` 页 `/supprot_direct` |
| 共享包 | `packages/errors` 的 29 个 `GPT_DIRECT_*` 错误码（`index.ts`）与 59 条中英文文案（`user-messages.ts`） |
| 迁移 | `legacy/V4`、`legacy/V5`、`V15`、`V16`（X）；`V18`、`V21`、`V23`、`V24`（GPT 直充） |
| 测试 | 11 个直充测试文件 + Java 差分机 `apps/api/tests/sms_gpt_parity/`；`scripts/tests/test_x_v15/v16_migration.py`、`test_gpt_v18_migration.py`、`test_seed_gpt_plans.py` |
| 文档 | 仅属直充/X 的验收记录与截图集（`docs/validation/2026-10-1*gpt*`、`*x-premium*`、`2026-10-10-screenshots/{gpt,x}-*.png` 等） |

保留：`test_removed_x_module.py`、`test_sms_gpt_*.py`（这 6 个文件是**接码模块**的用例，
名字里的 GPT 指产品线而非直充）。

公开仓库整理时又清掉了整段历史验收材料（`docs/validation/`、`docs/screenshots/` 下旧截图、
`docs/PROJECT_AUDIT_2026-10-11.md`、`docs/DELIVERY_REPORT.md`、`docs/PRIVATE_MIGRATION_BACKUP.md`、
部署记录与 `deploy/dujiao/validation/` 的镜像扫描证据）：这些文件既占十几 MB，也含真实部署域名与
开发机路径，不适合公开发布。本文档与 [本地启动](local-run.md) 是仅存的当前有效记录。

## 2. 装配层与契约

- `app.py`：删除 `GptDirectService` 装配、`GPT_DIRECT_POST_ACTION` 持久任务注册、
  `worker.gpt_sweep` 调度与 `gpt_sweep()`、两条 gpt 路由、`/api/v1/gptnotify/callback` 与
  `/api/v1/gpt-direct/webhook/*` 的 CSRF/体积豁免、全部 gpt-direct 限流桶。
- `common/catalog.py`：`KINDS={'SMS_REDEEM','CODEX_INVITE_REDEEM'}`；`productKind='GPT_DIRECT_REDEEM'` 现在 422。
- `common/inventory.py`：发放/作废不再处理 gpt 卡密；`findCdkInventoryIds` 的 SQL 去掉 gpt 分支。
- `common/mapper_sql.json`：删除 24 个 gpt 查询；`DurableJobMapper.page/count/retryFailed` 不再按
  `kind='GPT_DIRECT_POST_ACTION'` 过滤（持久任务队列保留为通用设施，`page` 不再返回 `keyId`）。
- `common/ops.py`：`/admin/stats/overview` 只剩 `sms`、`invitation` 两个模块、`sms`、`codex_phone`
  两组成本、`sms_uncertain`、`codex_settlement_review` 两条待办；`/admin/dashboard/summary` 移除 10 个 `gpt*` 字段。
- `security.py`：删除 `SCOPE_GPT` 与 `SupplierEndpointPolicy.require_gpt_configuration`（zovocard 专用）。
- `public_limits.py`：`CARD_TABLES` 只剩 `sms`。
- `frontend.py`：`PUBLIC_PAGES` 去掉 `supprot_direct`（该路径现在 404）。
- `common/runtime_catalog.json`：删除 `runtime.worker_gpt_direct_sweep_ms`；`apps/api/pyproject.toml` 删除 `smsinvit.gptdirect` package-data。
- 管理端：导航去掉「GPT 直充」组 6 项；`/gpt-direct/*` 路由全部删除；套餐页只剩两类、
  直充档位/支付国家/币种/资金上限/签发费字段全部移除；`zh-maps`、`jobs`、`notification-jobs`、
  `stats-overview`、`plans` 的直充文案与分支清理。
- 用户端：导航只剩「接码兑换」「Codex 邀请」；`GptDirectRedeemPanel`、直充专用恢复规则
  （`renewalStatusText`/`shouldRetainRecovery`/`TRANSIENT_RECOVERY_NOTICE`）与 `.gpt-*` 死样式删除；
  接码的深链、轮询、恢复逻辑逐字保留。

## 3. 顺带修正的两处既有缺陷

1. `apps/admin/src/pages/settings/runtime/page.tsx` 在交付快照里**不存在**，但 `main.tsx` 有
   `/settings/runtime` 懒加载路由、侧栏有「运行配置」、接码配置页两处链接指向它 —— `vite build`
   会因解析不到该模块失败（`scripts/release.py` 的 `SOURCE_NAME_EXCEPTIONS` 里还留着它的白名单）。
   处理：删除该路由、菜单项与两处失效链接，短信配置页改为说明该值属运行时配置；白名单清空。
2. `Scheduler.KNOWN`、`INTERVAL_KEYS` 与后台调度页里残留的 `worker.gpt_sweep` 一并删除，
   避免后台能写入一条永不生效的控制行。

## 4. 更名

旧名三档写法 `OLD_NAME`（首字母大写）、`old_name`（小写标识符）、`OLDNAME`（大写前缀）分别替换为
`SmsInvit`、`smsinvit`、`SMSINVIT`，覆盖 351 个文本文件（233 + 33 + 1677 处）与全部路径名：
`apps/api/old_name → apps/api/smsinvit`、`deploy/old_name → deploy/smsinvit`、
`@old_name/* → @smsinvit/*`、根包 `old_name-redeem → smsinvit-redeem`、
镜像/卷/库名 `smsinvit*`、环境变量 `SMSINVIT_*`、配置命名空间 `smsinvit.*`。
`pnpm install --frozen-lockfile` 通过，说明 `package.json` 与 `pnpm-lock.yaml` 的改名一致。

保留未改的字符串（不属于旧名）：Cookie 前缀 `lc_*`、卡密前缀 `LC-CDK-`（改它会改变已发卡密格式，
存量卡需要另做迁移）。

## 5. 验证

环境：Windows + Python 3.13（`.venv-verify` 临时 venv）、Node 26、pnpm 10.24.0、MySQL 用例跳过。

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| Python 编译 | `python -m compileall -q apps/api/smsinvit apps/api/tests scripts` | 通过 |
| 静态检查 | `ruff check apps/api/smsinvit apps/api/tests scripts`（只开 F821/F841） | `All checks passed!` |
| 类型检查 | `pnpm build:shared && pnpm typecheck` | 3/3 通过 |
| Lint | `pnpm lint`（eslint --max-warnings 0） | 3/3 通过 |
| 构建 | `pnpm build`（web + admin） | 3/3 通过 |
| 前端单测 | `pnpm test` | errors 2 / web 108 / admin 170，0 失败 |
| 导入图 + JSON | 自写校验：292 条 Python 导入、289 条 TS 相对导入、165 个 JSON | 全部可解析 |
| 路由面 | 用真实 `create_app()` 构造 FastAPI 并递归收集 151 条路由 | gpt-direct / x-api / gptnotify 均为 0；sms 27、invitation 83 |
| 迁移离线用例 | `pytest scripts/tests`（除 Linux-only 的 sandbox/scan_image） | 37 通过；4 失败均为 POSIX 权限/符号链接（见下） |
| 迁移测试 | `test_invitation_v17_migration.py`、`test_stats_v19_migration.py` | 11/11 通过（含 V1/V19/V20 在 SQLite 上重放） |
| 发布清单 | `release.selected_files()` + `scan_bytes()` 重建 `release-manifest.json` | 720 个文件，密钥扫描通过 |

后端全量用例（`pytest apps/api/tests`）与**原项目基线**对照，同一台机器、同一个 venv：

| | 通过 | 失败 | 跳过 |
| --- | --- | --- | --- |
| 本项目 | 1219 | 41 | 122 |
| 原项目（未改动） | 1636 | 43 | 162 |

失败集合比对：40 个完全相同（`os.O_DIRECTORY` 缺失、`st_mode` 位、符号链接/管道等 Windows 无
POSIX 语义导致，全部在 `invitation/phone_details`、`invitation/process`、`codex_cli_timing` 域）；
原项目多出的 3 个是 2 个直充用例加 `[OLDNAME_INVITATION_CA_BUNDLE]` 参数化；本项目"多出"的 1 个
是同一用例改名后的 `[SMSINVIT_INVITATION_CA_BUNDLE]`。**没有功能回归**。

未覆盖：需要真实 MySQL 的并发/租约/事务用例（122 个 skip）、Playwright 浏览器套件
（本机未装浏览器二进制）、以及 macOS/Linux 上的邀请执行沙箱用例。

## 6. 名称与已知限制

- 改名后配置键为 `smsinvit.*`，环境变量为 `SMSINVIT_*`，数据库名为 `smsinvit`。已有部署升级时
  需要同步改这些键，或保留旧键的迁移脚本（本次未提供，因为交付物是干净副本）。
- `V4/V5/V15/V16/V18/V21/V23/V24` 版本号留空档：`scripts/migrate.py` 现在校验"恰好等于 V1–V26
  减去这 8 个版本"，并对已有 Flyway 历史按版本映射而不是按序号映射。
- 管理端历史审计记录里若出现 `gpt.direct_*` 动作键或 `GPT_DIRECT_*` 错误码，会落到「未知（原值）」显示；
  这是删除文案表的必然结果，不再为已下线的业务保留标签。
