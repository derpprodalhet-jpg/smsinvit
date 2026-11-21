# codex_invit

从 `LocalFlow Register` 复制改造而来的独立项目。闭环是：

```
批量/单个注册邮箱  →  生成卡密  →  用户前台兑换（得到唯一未使用邮箱）
   →  用户把邮箱交给邀请方发邀请信  →  用户确认「我已发送邀请信」
   →  开始执行：等邀请信 → 注册该邮箱 → 登录 → 接受邀请 → Codex 授权取 RT
   →  Codex CLI 跑一个真实回合（sayhi）→ 成功 → 自动导出 SUB2API
```

> 成功判据只有一条：**Codex CLI 的完整回合跑通**（`turn.started` → 非空回复 → `turn.completed`，无 `turn.failed`）。
> SUB2API 导出失败**不计入失败**。

## 两个进程、两个端口

| 进程 | 默认地址 | 作用 |
|---|---|---|
| 管理后台 | `http://127.0.0.1:5093` | 邮箱/接码/代理池/卡密/注册结果/运行记录/仪表盘 |
| 用户前台 | `http://127.0.0.1:5094` | **只有**兑换 + 「我已发送邀请信」+「开始执行」+ 进度条 |

- 用户前台**只暴露用户接口**：`/api/registered`、`/api/settings/*`、`/api/cdk/*` 在 5094 上一律 404。
- 用户前台永远拿不到代理、密码、2FA、令牌、日志或内部错误码 —— 只有邮箱地址和一个脱敏阶段。
- **CDK worker 只在用户前台进程启动**，所以「同时最多跑 3 个」在全局成立（管理后台不启动 worker）。

## 安装与启动

```sh
cd codex_invit
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 管理后台（默认 127.0.0.1:5093）
.venv/bin/python start_admin.py

# 用户前台（默认 127.0.0.1:5094；要给外部用户访问才加 --host 0.0.0.0）
.venv/bin/python start_user.py
```

前端源码已构建到 `webui/static`（后台）与 `webui/static_user`（前台）。
改了前端源码要重新构建：

```sh
cd webui/frontend      && pnpm install && pnpm run build   # 后台 → webui/static
cd webui/frontend_user && pnpm install && pnpm run build   # 前台 → webui/static_user
```

## 首次配置顺序

1. **邮箱配置**：选邮箱来源（outlook / icloud_relay 走号池；CF 临时邮箱自建地址）。
   - CF 临时邮箱多一个可选的「取件代理」：**留空即自动跟随系统代理**
     （macOS 的 `scutil` 设置 → 再退回环境变量 `HTTP(S)_PROXY`）。本机靠 Clash/Surge
     上外网时，curl_cffi 默认 `trust_env=False` 会绕过代理直连 `*.workers.dev` 而超时，
     必须让它走代理才能过 CF 的 1010 拦截。
2. **导入邮箱**（池化来源）：每行一个，格式见「导入邮箱」页提示。
3. **接码配置**：填所用平台各自的 API Key（SmsBower / HeroSMS / SMSPool），按 §接码 调好国家/前缀/价格/库存规则。
4. **接码价格**：看 5s 任务算出的最优，必要时点「接口自检」确认 `physicalCount` 是否可用。
5. **代理池**：导入代理（服务端存储）。作业会自动轮换、按 409 规则淘汰坏 IP。
6. **自动导出**：启用 SUB2API（填 URL + API Key + group_ids）。
7. **邀请与执行**：填 `codex_binary` 路径、确认 CLI 可用（点「运行依赖自检」）。
8. **卡密管理**：生成卡密 → 复制/导出 → 交给用户。
9. 用户到 `http://<你的地址>:5094/` 兑换并执行。

## 接码：配置项与官方 API 参数对照

依据 [SMSBower For clients](https://smsbower.app/api?page=client)、[HeroSMS API](https://hero-sms.com/cn/api) 与 [SMSPool API](https://www.smspool.net/article/how-to-use-the-smspool-api-0dd6eadf4c)。
官方 `getNumber` 签名：

```
action=getNumber&service=$service&country=$country&maxPrice=$maxPrice&providerIds=$providerIds
&exceptProviderIds=$exceptProviderIds&phoneException=$phoneException&ref=$ref&userID=$userID&minPrice=$minPrice
```

> `$phoneException`：excluding prefixes for numbers. Separate with commas.
> Record format: country code and 3 to 6 digits of the mask (e.g. `7918,7900111`)。

| 页面字段 | 映射 |
|---|---|
| 平台模式 / 各平台 API Key | `auto` 用 5s 任务的最优平台；否则固定平台；Key 各自独立 |
| 默认国家 / 允许国家 / 排除国家 | `country` + 本地过滤（黑名单优先于白名单） |
| 排除的号码前缀 | **`phoneException`**（`52:66` 或 `5266` 两种写法都会归一化成 `5266`）；取号后本地再复核一次，命中就取消重租 |
| 最低价 / 最高价 | **`minPrice` / `maxPrice`** |
| 库存下限 | `count`（或实体卡 `physicalCount`）过滤 |
| 排除虚拟号 | 打开后按 **`physicalCount`** 计库存；接口没有该字段时会在页面上明确告警 |
| 只从/排除供货商 | **`providerIds` / `exceptProviderIds`** |
| 用任务最优覆盖 | 开：命中 SMS 时用最优平台+国家，`maxPrice = 最优价 + 容差`；关：用上面的手动配置 |

**5s 价格任务**（`webui/sms_prices.py`）：
- 已配置 Key 的平台各拉一次价格/库存（每 12 轮顺带查一次余额），落 `sms_prices`；
- 按当前配置过滤 + 平滑成功率打分 `score = w·归一价 + (1-w)·(1-成功率)`，取最小者为最优；
- 任务结果**不是摆设**：命中 SMS 时用的就是它（`effective_params()` 在「接码价格」页可见）。

**SMSPool**：在「接码配置」填 SMSPool API Key，选择「只用 SMSPool」或「自动择优」。
使用官方兼容地址 `https://api.smspool.net/stubs/handler_api`，继续使用上面的 SMS-Activate 服务/国家 ID
（OpenAI 服务 `dr`），不使用 SMSPool 原生 ID。取号仅用 `getNumber`，价格/余额用 `getPrices` / `getBalance`，
收码用 `getStatus`，退号用 `setStatus=8` 并检查平台确认。每个订单独立，不复用原平台缓存；
SMSPool 原生重发可能收费并生成新订单，本适配器只触发业务侧重发，不自动购买重发订单。
其兼容接口明确支持 `maxPrice` / `pool`，最低价通过现有报价筛选、前缀通过现有本地复核；
不透传未文档化的 `minPrice` / `phoneException` / `providerIds` / `exceptProviderIds`。
复用次数和供货商包含/排除配置仅适用于原有平台。现有注册、邀请、重试及导出流程保持原样。

## 代理池：轮换与 409 规则

- **轮换**：每次开始新任务，从「可用且 `use_count` 最小」的代理开始；同一作业的重试排除本次已用过的 IP。
- **409 生命周期**：连续 2 次 409 → **冻结 20 分钟**；解冻后再出现 1 次 409 → **永久不可用**；
  `warmup` 通过（该 IP 确认能拿到 `oai-did`）会把连续计数清零；永久不可用只能到代理池页手动「重置」。
- **同一 IP**：一次尝试只构造一个 `AuthFlow`，注册 → 登录 → 接受邀请 → Codex 授权 → CLI 全程复用同一个代理。
- **静默换 IP**：任何失败都不告诉用户，换一个没用过的代理重跑，最多 `invite_max_attempts`（默认 3）次；
  第 3 次仍失败才提示用户「未成功，请稍后重试」。
- **没有可用代理就不启动**：用户前台点「开始执行」会被拒绝并提示「当前繁忙，请稍后重试」，卡密保持可用。
- **卡密永不作废**（只有管理员能手动「作废」）。

## 卡密

- 一码一邮箱：池化来源从号池**随机**取一个未使用的；自建来源现场生成新地址并入库钉住。
- 该邮箱**就是**后续注册与 Codex 授权用的邮箱，全程不换。
- 池化来源邮箱不足时**整批拒绝**兑换，不做部分成功。
- 「使用记录」里能核对：邀请方邮箱、邀请信主题与收信时间、实际使用的代理、尝试/失败次数、失败原因。

## 运行记录

`runs.kind` 区分 `register`（单次注册）与 `codex_auth`（CDK 邀请作业 / 手动 Codex 授权）。
日志写在 `webui/logs/<run_id>.log`，页面上用 SSE 实时看。

## 测试

```sh
# 单元测试（离线、全打桩）
.venv/bin/python tests/run_all.py

# 端到端 API 验收（先把两个进程起好）
.venv/bin/python tests/e2e_api.py
```

覆盖：代理池 409 生命周期与轮换/预检查、接码前缀归一化与择优/生效参数、邀请信识别与链接访问、
CDK 作业顺序（同一 IP、静默换 IP、3 次失败不作废、防重放、无代理不启动）、CLI 回合判定、兑换与并发上限。

## 目录

```
auth_flow.py / fingerprint.py / http_client.py / sentinel*.py   # 注册与登录协议（复制自 LocalFlow Register）
sms_provider.py                                                 # 接码 provider（改造：参数透传/前缀过滤/多平台/统计钩子）
mail_providers/                                                 # 邮箱 provider（扩展：recent_messages / pin_mailbox / 邀请信等待）
webui/
  app.py          管理后台 API
  user_app.py     用户前台 API（唯一跑 CDK worker 的进程）
  cdk.py          卡密 + 并发队列（≤3）+ 排队
  codex_auth.py   合并作业编排（等邀请信→注册→登录→接受邀请→授权→sayhi→导出）
  codex_cli.py    Codex CLI sayhi 运行与成功判定
  invite_mail.py  邀请信识别 / 链接提取 / 带登录态访问
  mail_alloc.py   兑换时的邮箱分配
  sms_prices.py   5s 价格任务 + 择优 + 生效参数 + 接口自检
  proxy_pool.py   服务端代理池（轮换 + 409 生命周期 + 预检查）
  db.py           SQLite（号池/注册结果/运行/设置/卡密/价格/统计/代理池/Codex 凭证）
  frontend/       管理后台前端（Vue3 + Element Plus）
  frontend_user/  用户前台前端（Vue3 + Element Plus，单页）
tests/            单元测试
```
