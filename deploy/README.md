# 兑换站独立部署

本次仅交付分离后的 `smsinvit-redeem`。兑换站使用 `smsinvit/compose.yml`，两项业务（接码 / Codex 邀请）共用该站自己的 MySQL 8.4。独角继续使用 `/srv/workspace/dujiao/dujiao-next` 原目录及原有独立服务器、数据库和配置，本次不修改或迁移独角。兑换站不共用独角的网络、密钥、数据卷或数据库账号。

## 发布与构建

源码归档由 `scripts/release.py` 的白名单生成，内含每个文件的 SHA-256 清单。不会读取或包含本地数据库、`.env`、授权文件、运行日志、密钥、Docker 登录配置、依赖目录和构建缓存。桌面原 `codex_invit` 不属于构建路径。

```bash
python scripts/release.py --check
python scripts/release.py --version v1.0.0-python --output artifacts/smsinvit-redeem-v1.0.0-python.tar.gz
python scripts/build_images.py --version v1.0.0-python --architectures amd64,arm64
```

基础镜像使用 `deploy/images.lock.json` 中的摘要，Python 使用带哈希的 `apps/api/requirements.lock`，前端使用冻结的 pnpm 锁。只构建本项目用户前台和 React + Ant Design 统一后台，邀请的十项管理菜单全部位于统一后台。邀请副本的旧 Vue 前端、用户前端和旧静态资源不进入源码发布包或运行镜像，Python 业务模块和既有 `/api/v1/admin/invitation/native/api/*` 接口保留。旧 `/admin/invitation/native` 重定向至原生概览，子路径返回 404。Codex CLI 固定为源项目使用的 `0.160.0`。构建结果和无网络验证保存于 `artifacts/build-evidence.json`；不要将构建日志写入源码包。只有经过实际构建与检查的架构可标记为通过。

兑换站运行用户为 `10001:10001`。部署环境示例默认 amd64 标签，arm64 服务器必须改用对应的 arm64 标签。下载条件受限时可显式使用 `--verified-local-cache`，该路径仍核对官方清单、每层压缩摘要和解压摘要。本次交付包含兑换站、MySQL、Caddy 各两个架构的六个镜像；保留的独角副本、镜像及构建说明属于历史附加资料，不用于原独角的部署或验收。

`scripts/scan_image.py` 保存高危与严重漏洞报告，并区分通过、有告警和无法扫描。官方 Trivy 离线库可由 `scripts/fetch_trivy_db.py --cache-dir /secure/scanner-cache` 取得；每层摘要均校验，扫描拒绝超过 48 小时的数据库。运行 `scan_image.py --image <已构建标签> --trivy /path/to/trivy --cache-dir /secure/scanner-cache --export-rootfs` 可扫描从未启动的临时容器导出的运行文件系统，记录精确镜像 ID。此模式覆盖已安装运行包；删除的历史镜像层不在此模式范围内。[Trivy 官方离线说明](https://trivy.dev/docs/latest/guide/advanced/air-gap/)。

需离线传输镜像时，使用 `scripts/export_images.py --image <标签> --output-directory /secure/new-image-export --build-evidence <构建证据>`，多架构可重复传入 `--image`。工具只导出匹配构建证据和普通用户配置的本地镜像，附带每个归档的 SHA-256；不启动服务或挂载运行数据。服务器导入前应核对归档和清单。

## 兑换站新服务器

迁移已有数据必须使用原有库存加密、指纹和 JWT 密钥。密钥变更会导致历史密文无法解密或卡密指纹无法匹配。将三个值写入服务器上 `0600` 私有 JSON 文件，字段为 `inventory_encryption_key`、`inventory_fingerprint_key`、`jwt_secret`，不要提交或打包该文件。仅空白新站可显式选 `--new-install`。

```bash
python scripts/prepare_deployment.py --directory deploy/smsinvit/private --domain redeem.example.com --retained-secrets-file /secure/retained-secrets.json
```

生成工具只接受全新的私有目录，不覆盖已有密钥。Linux 上以管理员身份执行可准备容器文件归属；否则手工将 `private/runtime.yaml` 和 `private/migration.json` 设为 `10001:10001`、`0600`，将 `private/mysql-tls/`、`private/init/` 和其 SQL 文件设为 MySQL UID `999` 可读（目录 `0750`、SQL `0640`），`private/mysql-root-password`、`server-key.pem` 归 MySQL UID `999` 且 `0400`，其它证书 `0644`。私有 CA 的签名密钥不挂入任何容器。

复制 `environment.example` 为服务器上的 `environment`，填写本站架构的兑换站与 `smsinvit-mysql` 镜像标签、本站域名、证书邮箱及对应架构的 `smsinvit-caddy` 标签。MySQL 支持镜像保持 8.4.11，卸载未使用的 mysql-shell、删除 gosu，直接以 UID999 启动；两个架构均已实测全新卷、TLS 1.3、V1–V8、64 表备份恢复和重启持久化。Caddy 支持镜像由锁定官方 2.11.7 内容派生，仅移除文件绑定能力并准备 UID10001 的 `/data`、`/config` 属主，二进制字节未改。新命名卷继承该属主；导入已有证书和配置卷后先设为 UID/GID10001 可写。全部能力删除，低端口由容器命名空间 `net.ipv4.ip_unprivileged_port_start=0` 允许，宿主机 sysctl 不被改动。环境文件只包含公开部署元数据。数据库、API、后台和四项业务密钥均置于私有文件或数据库加密字段。

```bash
docker compose --env-file environment -f compose.yml up -d mysql
docker compose --env-file environment -f compose.yml run --rm maintenance
docker compose --env-file environment -f compose.yml up -d api
```

在 `deploy/smsinvit/` 执行上述操作。应用启动不会创建或修改表结构。迁移工具只在显式运行时安装新库，或验证已有历史、脚本校验与表结构后追加缺失版本。校验冲突、失败历史和未经确认的无历史旧库会拒绝执行，禁止强行覆盖。无历史但完整且匹配的库只有显式 `--adopt-existing-schema` 才可登记。V8 的邀请执行占用记录在数据库重启后仍保留；崩溃占用不会被自动抢占，避免旧进程还在运行时重复执行。V10 持久保存领取请求及卡密集合的 HMAC 关联，响应丢失后凭独立恢复密钥查询原结果；结果不明的请求保留待复核，不自动重新分配邮箱。

上述阶段只启动数据库、维护和没有宿主机端口的私网 API。`worker-enabled=false` 只关闭后台 worker；接码同步兑换接口仍可能付款，因此在所有数据恢复、冲突选择、域名、两项业务配置与进程隔离检查完成前，不启动公开 proxy，不切换生产 DNS。检查完成后显式执行 `docker compose --env-file environment -f compose.yml up -d proxy`，最后才切换 DNS 和恢复用户流量；需要后台任务时另将私有配置的 `smsinvit.worker-enabled` 改为 `true` 并重建 API。

Codex 邀请拆成「账号工厂（预先注册到 2FA、停住）」和「CDK 兑换 + 启动执行」两段，因此邀请侧有两个**互相独立**的开关键：`smsinvit.invitation-worker-enabled`（用户能不能点「开始」）与 `smsinvit.invitation-pool-enabled`（账号工厂要不要自动补号）。只把 pool 置 `true` 也会启动邀请执行进程，但用户端仍点不了「开始」，可以先把待兑换账号池养起来。两者各有自己的并发额度（工位 1–16 / 17–32）与参数（`pool_*` 与 `invite_*`），后台「Codex 邀请 → 服务配置」里的账号池开关保存后立即生效；补号会消耗代理、邮箱库存与接码余额，开启前先核对代理池与邮箱来源（只支持池化来源）。

邀请执行要求 Linux Landlock ABI 至少 6、seccomp 和项目的网络隔离助手；版本与执行边界自检失败会拒绝启动邀请 worker。不增加容器特权、关闭默认安全策略或绕过助手。镜像构建检查与内核隔离检查分别记录，仿真架构的构建成功不证明其内核隔离已通过。

后台接码与邀请分别保留自己的配置，开放相应业务前核对各自的供应商与代理。

部署出站授权须在 `environment` 配置准确的 `host:port`，例如 `SMSINVIT_INVITATION_ALLOWED_HOSTS=mail.example.com:443`（自定义邀请邮箱/API域）、`SMSINVIT_INVITATION_PROXY_HOSTS=proxy.example.com:8443`（运营方明确批准的代理）、`SMSINVIT_SUPPLIER_ALLOWED_HOSTS=supplier.example.com:443`（接码自定义供应商）。列表以逗号分隔，无协议、路径、通配符或密钥。邀请两个授权变量由 `os.environ` 读取，写进 runtime YAML 不会替代这些环境变量；本模板已显式传给 API。自定义业务目标仍须 HTTPS、公开 IP、完整证书与主机名校验；私网代理仅显式授权精确代理端点。

后台地址字段分别是：接码的上游 `baseUrl`；邀请的邮箱/目标 URL 在邀请配置中，出站域名必须与上面的邀请授权对应。每个服务的密钥和业务设置仍单独保存在数据库，不把密钥写进上述公开环境文件。

若确有运营方批准的私有 CA，将**仅证书的 PEM** 信任 bundle 保存为 `private/outbound-ca.pem`，UID10001 可读，最多 1MiB；不含私钥，且需包含全部实际目标所需的受信根。显式加 `-f compose.ca.yml` 到 API 的 compose 命令，才挂载只读叶文件并设置 `SMSINVIT_INVITATION_CA_BUNDLE` / `SMSINVIT_CA_BUNDLE`。默认不加载该覆盖文件，使用系统信任库。没有关闭 TLS 或主机名验证的部署选项。变更环境或信任 bundle 后重建 API，在公开 proxy 开放前完成验证。
结果不明的支付只查询原单，不自动创建新单；邀请成功沿用实际非空 `agent_message` 判据，不表示奖励已到账。

源数据库的后台运行时配置优先于私有 YAML。换域名时，在 API 停止状态下显式执行 `scripts/configure_site.py --web-origin https://redeem.example.com`，先预览，再加 `--apply`；传入维护账号私有凭据和 CA。该工具只更新公网 API 基址和 CORS 两项公开元数据并写审计，不更新业务配置。API 基址填域名，不添加 `/api`；业务自行追加回调路径。完成后启动或重启 API。

API 无宿主机端口，MySQL 的维护端口只绑定服务器回环地址，业务流量走私有内网，只有 HTTPS 代理公开。MySQL 还单独加入维护 bridge，API/代理不会加入该网络；其唯一宿主机发布仍为 `127.0.0.1`。初始化临时 mysqld 只开启 socket，健康检查使用 TCP+VERIFY_IDENTITY，待账号初始化及最终 TCP 服务可用才就绪。可信代理只绑定代理容器地址；生产 Cookie 使用 Secure，CORS 使用本站具体域名，数据库证书和主机名均校验。运行账号只有增删改查，迁移和备份使用不同账号。

## 备份与恢复

维护工具需 Python 3.12 依赖，以及官方 MySQL 8 客户端 `mysqldump`/`mysql`。使用 `0600` 私有凭据文件与服务器 CA；密码不得写在命令参数中。备份文件是敏感数据，单独加密保存，绝不进入源码包。

```bash
python scripts/backup.py --credentials-file /secure/backup.json --host 127.0.0.1 --ca-file /secure/mysql-ca.pem --output /secure/backup.sql
python scripts/restore.py --credentials-file /secure/migration.json --host 127.0.0.1 --ca-file /secure/mysql-ca.pem --input /secure/backup.sql
```

恢复必须使用显式选择的空目标库，并核对伴随清单的大小、SHA-256、表清单、迁移历史和表结构。恢复工具不覆盖非空数据库。复制旧邀请 SQLite 时，先在项目外复制数据库与 WAL/SHM 并验证源文件未变，只允许打开复制件，使用 `scripts/sqlite_snapshot.py`；原项目任何文件均不可写入或运行。

## 原独角项目

独角保留在 `/srv/workspace/dujiao/dujiao-next`，继续使用原项目的部署和维护方式。本说明只用于兑换站，不要求创建 `dujiao-server`、替换独角镜像或迁移其配置和数据。源码中保留的 `deploy/dujiao/` 及相关证据属于此前额外副本工作的历史资料，不是本次部署入口，也不代表原独角已完成对应安全验收。
