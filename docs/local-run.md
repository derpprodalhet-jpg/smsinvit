# 本地启动（Windows 验证机实测）

本页记录在本机把 SmsInvit 跑起来的完整步骤与实测结果。仓库自带的入口是
`scripts/local_preview.py`：**隔离库 + 模拟接码供应商**，不连任何真实上游，也不需要真实密钥。

## 1. 依赖

| 组件 | 本机实际取值 | 说明 |
| --- | --- | --- |
| Python | 3.13.15 | 项目要求 >=3.12 |
| Node / pnpm | 26.13.1 / 10.24.0 | `pnpm install --frozen-lockfile` 通过 |
| MySQL | 8.4.13（官方 winx64 免安装 zip） | 官方 CDN zip，解压到项目外目录 |

MySQL 免安装部署（Windows，无需管理员权限、不装服务）：

```powershell
# 下载（dev.mysql.com 对本机 403，用官方 CDN 直链）
curl.exe -L -o D:\deepseek\mysql-8.4.13-winx64.zip `
  'https://cdn.mysql.com//Downloads/MySQL-8.4/mysql-8.4.13-winx64.zip'
tar.exe -xf D:\deepseek\mysql-8.4.13-winx64.zip -C D:\deepseek\mysql-8.4.13

$base = 'D:\deepseek\mysql-8.4.13\mysql-8.4.13-winx64'
$data = 'D:\deepseek\mysql-data'
& "$base\bin\mysqld.exe" --initialize-insecure --basedir="$base" --datadir="$data" --console
& "$base\bin\mysqld.exe" --basedir="$base" --datadir="$data" --port=13308 `
  --bind-address=127.0.0.1 --mysqlx=OFF --skip-log-bin --console
```

首次初始化后设置 root 口令并建预览库（口令写进 `data/test-private/mysql.json`，该目录被 `.gitignore` 忽略）：

```powershell
& "$base\bin\mysql.exe" --host=127.0.0.1 --port=13308 --user=root `
  --execute="ALTER USER 'root'@'localhost' IDENTIFIED BY '<NEW_PASSWORD>'; CREATE DATABASE smsinvit_preview_20261018 CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
```

> `mysql.exe` 的短选项 `-h127.0.0.1` 在 PowerShell 下会被拆开，必须用 `--host=` / `--port=` 长选项。

## 2. 项目依赖与前端产物

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r <(pyproject 的依赖)   # 见下：按 pyproject 精确版本安装
$env:CI='1'; pnpm install --frozen-lockfile
pnpm build:shared      # packages/* -> dist（web/admin 的类型解析依赖它）
pnpm build             # apps/web/dist + apps/admin/dist
```

后端依赖版本以 `apps/api/pyproject.toml` 的 `dependencies` 为准（如 `fastapi==0.142.2`、
`SQLAlchemy==2.1.3`、`cURL_cffi==0.16.3`、`pyotp==2.10.0`）。注意 `curl_cffi` 是
`smsinvit.invitation.safety` 的导入依赖，缺它会直接 ImportError。

## 3. 启动

```powershell
$env:PYTHONUTF8='1'                 # 项目文件为 UTF-8；Windows 默认 GBK 会让 read_text 解码失败
.\.venv\Scripts\python.exe scripts/local_preview.py
```

`local_preview.py` 已改成跨平台，可用环境变量覆盖（默认值取自原脚本）：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `SMSINVIT_PREVIEW_MYSQL` | 读 `data/test-private/mysql.json` | `{"host","port","user","password"}` JSON |
| `SMSINVIT_PREVIEW_HOME` | 系统临时目录下 `smsinvit-local-preview` | 密钥文件、静态副本、邀请数据目录 |
| `SMSINVIT_PREVIEW_DATABASE` | `smsinvit_preview_20261018` | 预览库名 |
| `SMSINVIT_PREVIEW_PORT` | `3010` | 监听端口（仅 127.0.0.1） |
| `SMSINVIT_PREVIEW_ADMIN_USER` | `admin` | 预览管理员用户名 |
| `SMSINVIT_PREVIEW_ADMIN_PASSWORD` | `123456` | 预览管理员口令 |
| `SMSINVIT_PREVIEW_ADMIN_EMAIL` | `admin@example.test` | 预览管理员邮箱 |

脚本会：跑迁移建表 → 生成三把密钥 → 把 `apps/{web,admin}/dist` 复制成静态目录 →
把供应商 HTTP 全部替换为 MockTransport（503）→ 写入预览套餐 `LOCAL-PREVIEW-SMS`、卡密
`PREVIEW-SMS-001…007` 与管理员 `admin / 123456`（每次启动都把这组用户名、邮箱与口令哈希写回，
所以即使从后台改过也不会失效）→ 起 uvicorn。

## 4. 页面实测

headless Chrome（本机 `C:\Program Files\Google\Chrome\Application\chrome.exe`）截的页面：

| 页面 | 截图 |
| --- | --- |
| 接码兑换 `/supprot_sim` | [local-sms-page.png](screenshots/local-sms-page.png) |
| Codex 邀请 `/supprot_codex` | [local-codex-page.png](screenshots/local-codex-page.png) |
| 后台登录 `/admin/` | [local-admin-login.png](screenshots/local-admin-login.png) |

前台导航只有「接码兑换」「Codex 邀请」两项，品牌为 SmsInvit；后台登录页为「SmsInvit Admin」＋算术验证码。

## 5. 实测结果（2026-11-21，本机）

| 检查 | 结果 |
| --- | --- |
| `/`、`/supprot_sim`、`/supprot_codex`、`/invitation` | 200（SPA，静态资源 `/assets/index-*.js` 200，215 KB） |
| `/admin/` | 200，响应内注入 `<base href="/admin/" data-admin-base>` |
| `/supprot_direct`、`/api/v1/gpt-direct/page-content`、`/api/v1/x-api/preflight` | 404（已下线） |
| `/api/v1/health` | 200 |
| 接码兑换链路 | `POST /api/v1/sms/redeem {PREVIEW-SMS-004}` → 号码 `+12025550123` → `fetch-code` → 轮询 12 秒拿到模拟验证码 `123456`；同一 CDK 重提返回同一会话（`SUCCESS`） |
| 管理端登录 | `GET /api/v1/admin/auth/captcha`（算术题）→ `POST /api/v1/admin/auth/login {identifier,captchaToken,captchaAnswer}` → 200 发 access token；账号 `admin / 123456`，`roles=['SUPER_ADMIN']`，`needTotp=False` |
| `/api/v1/admin/stats/overview` | 200，`modules=['sms','invitation']`、`costs=['sms','codex_phone']`、`pending=['sms_uncertain','codex_settlement_review']` |
| `/api/v1/admin/dashboard/summary` | 200，27 个字段，`gpt*` 字段 0 个 |
| `/api/v1/admin/schedulers` | 200，任务 `worker.process / worker.heartbeat / worker.recovery / worker.sms_sweep / notification.poll` |
| 创建 `GPT_DIRECT_REDEEM` 套餐 | 422 `VALIDATION_ERROR`「套餐类型须为接码或 Codex 邀请。」 |
| 创建 `SMS_REDEEM` 套餐 | 200 |

## 6. 停止与清理

```powershell
# 停预览（pid 见 `Get-NetTCPConnection -LocalPort 3010`）
Stop-Process -Id (Get-NetTCPConnection -State Listen -LocalPort 3010).OwningProcess
# 停 MySQL
Stop-Process -Id (Get-NetTCPConnection -State Listen -LocalPort 13308).OwningProcess
```

预览产生的 `.venv/`、`node_modules/`、`data/`、`apps/*/dist`、`packages/errors/dist` 全部在
`.gitignore` 内，删除它们即可回到干净源码树；`D:\deepseek\mysql-8.4.13` 与
`D:\deepseek\mysql-data` 在项目外，不影响仓库。

## 7. 边界

预览是**模拟环境**：供应商调用被 MockTransport 拦成 503，验证码由本地 `PreviewSms` 生成，
线路检测同样为模拟。要接真实上游，需在后台「接码 → 服务配置」填入真实供应商 baseUrl/密钥、
导入真实账号池与卡密，并按部署文档配置 `SMSINVIT_SUPPLIER_ALLOWED_HOSTS` 等出站白名单；
邀请执行另需 Linux（Landlock/seccomp 边界）。

预览口令 `123456` 只有 6 位，而**后台「员工管理」的建号与重置口令接口仍要求 ≥8 位**
（`staff.py` 的 `bounded(password,128,True,minlen=8)`，`test_common_security` 有回归断言）。
所以这组口令只能用于预览登录；要在后台改口令请用 ≥8 位，或从
`SMSINVIT_PREVIEW_ADMIN_PASSWORD` 换一个更长的值重启预览。真实部署不要沿用弱口令。
