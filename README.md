# SmsInvit · 短信接码与 Codex 邀请兑换站

一套自建的服务兑换站：用卡密（CDK）给用户发放**短信验证码接收**能力，或把预先注册好的
GPT 账号通过 **Codex 邀请**交付给用户。前台用户自助兑换，后台统一管理套餐、卡密、成本与运行记录。

项目原本是三条业务线（接码 / GPT 直充 / Codex 邀请），**GPT 直充与 X Premium 已整体下线**，
只保留接码与 Codex 邀请；迁移、任务、权限与前端入口一并清理，见
[模块下线记录](docs/CHANGELOG-2026-11-21-module-removal.md)。

> 这是自托管工具，不含任何上游账号、密钥或真实数据。对外提供服务前请自行确认目标站点的
> 服务条款与当地法律，只在你拥有或已获授权的范围内使用。

## 功能

**接码兑换（SMS）**

- 卡密兑换后分配号码，用户在目标站点触发短信，回来取验证码；同号多码、换号、取消补偿齐全。
- 号码尝试次数、取消宽限期、候选价带与排除前缀都按套餐配置；号码全局只租一次。
- 上游采购与激活成本按会话记账，后台按币种分行展示，未定价的行明确标「未定价」而不是当 0。

**Codex 邀请**

- **账号工厂**：用代理把池化邮箱注册成 GPT 账号、绑好 2FA 就停住，产出「邮箱 / 密码 / 2FA /
  Access / Session、无 RT」的待兑换号池；池内低于阈值时按独立开关与并发自动补号。
- **CDK 兑换**：从池中取号并立即绑定，同一邮箱不会被第二张卡密取走；池空提示重试，池内不足则
  询问是否继续，只消耗与可用数量相等的卡密。
- 执行链路：查邀请信 → 用池中会话恢复登录 → 接受邀请 → Codex OAuth 换取 RT → 接码 →
  CLI → 导出；结果不明时保留原单与写前状态，绝不自动重放。

**管理后台**

套餐与卡密批次（含独角「批量录入 / CSV」导入格式）、兑换单据、邀请运行与失败复核、
邮箱池、代理池与 409 双锁、接码价格与余额、审计日志、持久任务与定时任务、后台 IP 封禁、通知渠道。

## 架构

```
apps/api        FastAPI 服务（Python 3.12+）：接码、邀请、公共与管理 API，同源提供前台与后台静态资源
apps/web        用户端 SPA（React 18 + Vite + Tailwind）：/supprot_sim、/supprot_codex
apps/admin      管理端 SPA（React 18 + Ant Design Pro）：/admin/
packages/*      共享 TS 包（错误码与中英文文案、tsconfig）
vendor/codex_invit  Codex 邀请引擎（注册、邮箱、Codex OAuth、CLI 执行、导出）
migrations      Flyway 风格 SQL：V1–V26（含下线的 V4/V5/V15/V16/V18/V21/V23/V24 空档）
scripts        迁移、备份/恢复、发布打包、镜像扫描、Codex 耗时测量、本地预览
deploy          生产 compose、Caddy、环境变量样例与部署说明
```

技术栈：Python 3.12+ / FastAPI / SQLAlchemy / MySQL 8.4；React 18 / TypeScript / Vite / Turbo / pnpm。

## 快速开始

### 依赖

- Python ≥ 3.12
- Node ≥ 24，pnpm 10.24
- MySQL 8.4（生产；本地预览也要一个实例）
- Codex 邀请的真实执行需要 Linux（Landlock ABI 6 + seccomp），Windows / macOS 只能跑模拟或前台

### 安装

```bash
python -m venv .venv
.venv/bin/pip install -r apps/api/requirements.lock      # Windows: .venv\Scripts\pip
pnpm install --frozen-lockfile
pnpm build:shared && pnpm build                          # 产出 apps/{web,admin}/dist
```

### 本地预览（隔离库 + 模拟供应商，不连任何真实上游）

```bash
# 指向一个一次性 MySQL：环境变量或 data/test-private/mysql.json（该目录已被 .gitignore 忽略）
export SMSINVIT_PREVIEW_MYSQL='{"host":"127.0.0.1","port":13306,"user":"root","password":"..."}'
python scripts/local_preview.py           # Windows 需先 set PYTHONUTF8=1
```

- 前台 <http://127.0.0.1:3010/>、后台 <http://127.0.0.1:3010/admin/>
- 管理员 `admin / 123456`（每次启动写回，可被 `SMSINVIT_PREVIEW_ADMIN_*` 覆盖）
- 模拟卡密 `PREVIEW-SMS-001` … `PREVIEW-SMS-007`，取号后约 8 秒出现验证码
- 预览会把供应商 HTTP 全部替换为 503 MockTransport，线路检测同样为模拟

细节（MySQL 免安装部署、环境变量、实测结果、停止命令）见 [本地启动](docs/local-run.md)。

### 生产部署

见 [部署说明](deploy/README.md)：统一镜像、Caddy 入口、私有网络 MySQL、TLS 与首次建库/备份恢复。
生产必须提供 `SMSINVIT_INVENTORY_ENCRYPTION_KEY`、`SMSINVIT_INVENTORY_FINGERPRINT_KEY`、
`SMSINVIT_JWT_SECRET` 三个密钥，并把 `worker-enabled` 显式打开才会跑后台任务。

## 配置要点

| 项 | 说明 |
| --- | --- |
| `SMSINVIT_CONFIG_FILE` | 私有运行时配置（YAML/JSON），`SMSINVIT_*` 环境变量优先 |
| `SMSINVIT_SUPPLIER_ALLOWED_HOSTS` | 接码上游出站白名单，`host:port` 精确匹配；未列入的地址在发请求前拒绝 |
| `SMSINVIT_INVITATION_ALLOWED_HOSTS` | 邀请侧出站白名单（邮箱 API、导出面板等），同样精确到 `hostname:443` |
| `smsinvit.worker-enabled` | 后台调度总开关；关闭时同步兑换仍可用 |
| `smsinvit.invitation-worker-enabled` / `-pool-enabled` | 邀请执行 / 账号工厂补号，两个独立开关 |
| 供应商 baseUrl、密钥、价带 | 后台「接码 → 服务配置」与「套餐」页维护，落库后加密存储 |

运行时配置项清单在 `apps/api/smsinvit/common/runtime_catalog.json`，供应商地址与密钥只从
部署白名单和数据库读取，不接受前端传入。

## 安全模型

- 库存密钥、供应商密钥、账号凭据、TOTP secret 一律加密落库；卡密明文只在具备导出权限的详情里出现，
  每次读取写审计，响应 `Cache-Control: no-store`。
- 卡密用 HMAC 指纹做全局唯一性登记，号码与邮箱使用指纹去重，禁止跨模块重复兑付。
- 后台登录：账号密码 + 算术验证码 + 可选 TOTP；按账号 5 次 / 15 分、按 IP 50 次 / 15 分限流，
  连续 5 次密码错误封 IP 24 小时，封禁作用于整个 `/api/v1/admin/*`。
- Cookie 会话 + CSRF 双提交校验；响应统一 CSP（无内联脚本）、`X-Frame-Options: DENY`、
  `Referrer-Policy: no-referrer`，跨域来源白名单可配。
- 出站请求走端点策略（协议 + 精确 host:port + 公网地址校验 + DNS 预算），邀请执行子进程在
  Landlock/seccomp 边界内运行并只注入本次代理。
- 数据库使用最小权限账号，DDL 与备份使用独立账号；`scripts/release.py` 打包前做密钥扫描。

## 数据与迁移

```bash
python scripts/migrate.py --status            # 只校验历史与表结构
python scripts/migrate.py                     # 追加缺失版本
python scripts/migrate.py --adopt-existing-schema   # 显式登记已存在的完整旧库
```

迁移是前向的、带 Flyway 校验和：已发布脚本的字节被冻结（`scripts/tests/migration_fixtures.py`），
历史不连续、校验冲突或失败记录都会拒绝执行。下线版本的版本号保留空档，不重排。
备份与恢复见 `scripts/backup.py` / `scripts/restore.py`。

## 测试与验证

```bash
pnpm typecheck && pnpm lint && pnpm test && pnpm build      # 前端：类型 / lint / 单测 / 构建
python -m pytest apps/api/tests scripts/tests               # 后端：需自行安装 requirements-test
ruff check apps/api/smsinvit apps/api/tests scripts         # 只开 F821/F841
```

需要真实 MySQL 的并发、租约、事务与迁移用例默认跳过，显式提供 `data/test-private/mysql.json`
（或 `SMSINVIT_TEST_PRIVATE_CONFIG`）才会连一个一次性库。

本机（Windows 11 / Python 3.13 / Node 26 / pnpm 10.24）最近一次实测：
前端 280 个单测通过、typecheck/lint/build 全绿；后端 1219 通过 / 122 跳过，
其余 41 个失败全部是需要 POSIX 语义（`os.O_DIRECTORY`、文件权限位、符号链接）的用例，
在 Linux 上运行不受影响。

## 目录约定

- 私有材料（`.env*`、`data/`、`private/`、密钥、SQLite、日志）不进仓库，`.gitignore` 已覆盖；
  发布打包另有白名单（`scripts/release.py`）与密钥扫描。
- 迁移脚本一经发布不再改动；需要修正时新增版本。
- 前端文案集中在 `packages/errors`（错误码）与 `apps/admin/src/lib/zh-maps.ts`（枚举中文），
  未知枚举显示「未知（原值）」而不是英文或空白。

## 已知限制

- 邀请的真实执行依赖 Linux 沙箱能力；Windows / macOS 只能跑模拟或用户前台。
- 后台「员工管理」的建号与重置口令要求 ≥8 位；本地预览的 `123456` 只能用于登录。
- 历史审计里若出现已下线业务的动作键或错误码，会显示为「未知（原值）」，不再保留对应标签。
- 项目同时支持中文文案与英文文案表，但界面默认中文。

## 许可与第三方

本项目以 [MIT 许可证](LICENSE) 发布，可自由使用、修改、分发与商用，保留版权与许可声明即可。

第三方组件与来源说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；vendored 的
`vendor/codex_invit` 与 `patches/` 下的补丁按各自原始许可使用。
