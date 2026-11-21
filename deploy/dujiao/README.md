# 独角独立部署包

本包只含源码、部署模板、固定依赖锁和公开构建工具，数据库、配置密钥、授权、日志均另行私有迁移。业务源码来自原目录的一致性副本；依赖安全补丁和容器权限只修改副本。该服务器不连接兑换站的数据库、网络或数据卷。

使用 Python 3.12 安装工具依赖：`python -m pip install --require-hashes -r deploy/tool-requirements.lock`。在源码包根目录执行 `python scripts/build_images.py --project dujiao --architectures amd64,arm64`，会使用 `deploy/images.lock.json` 中的官方摘要及冻结前端/Go依赖，生成独立的两架构普通用户镜像和构建证据。Go builder 固定 1.27.1。公共镜像下载受限时可显式加 `--verified-local-cache`，所有 OCI 摘要仍核验。临时下载代理只用于构建，不进入运行配置。

配置必须保留原独角密钥、管理员和业务设置。将已设置 owner-only 权限的正式配置，通过 `scripts/prepare_dujiao.py --directory deploy/private --config-file /secure/retained.yml` 逐字复制到新的私有目录。Linux 管理员执行时设置 `config.yml` 归属 UID/GID10001；否则手工设置该归属和 0600。填写自己的生产域名、CORS、可信代理 `172.31.78.10/32`，Redis 主机 `redis`。密钥不出现在环境文件或源码归档中。

数据使用该项目自己的 SQLite 卷，Redis、上传和日志卷均独立。恢复原数据后、启动前将卷内文件归属设置为 `10001:10001`，并核对旧库一致性与管理员登录；新命名卷会复制镜像中该用户的目录归属。

备份前停止商店 API 和后台任务写入，再处理 SQLite、上传、配置及 Redis 的一致性。使用 `python scripts/sqlite_snapshot.py --source /secure/store.db --output-directory /secure/new-snapshot` 先复制数据库及 WAL/SHM/journal 文件，只打开副本做完整性校验和合并，原库不被 SQLite 打开。复制上传和配置到同一私有备份目录；Redis 停写后保留其 RDB/AOF。未知支付结果和队列中断任务须核对原单，恢复时不自动重放不明付款。

本服务器单独生成并离线保管备份密钥：`python scripts/private_archive.py keygen --keys-file /secure/shop-backup-key.json`。随后 `python scripts/private_archive.py encrypt --keys-file /secure/shop-backup-key.json --input /secure/shop-backup-files --output /secure/shop-backup.enc` 加密整个一致性目录，恢复用 `restore` 并指定新的空目标路径。密钥和明文文件为 0600，密钥不打印、不打入源码或镜像，也不与兑换站共用。认证失败、篡改、路径穿越或已有目标均拒绝恢复。工具的最小哈希锁包含 requests 与 cryptography 的完整传递依赖，独角工具无需另一服务器的代码或数据库。

在 `deploy/` 将 `environment.example` 复制为部署环境文件，选择服务器对应的 amd64 或 arm64 独角、`dujiao-redis` 镜像标签，填写对应架构 `smsinvit-caddy` 镜像标签、域名和证书邮箱，先执行 `docker compose --env-file environment -f compose.yml up -d redis shop`，此阶段商店无宿主机端口；完成恢复、配置、旧管理员登录与队列核对后，最后显式执行 `docker compose --env-file environment -f compose.yml up -d proxy`，再切换生产 DNS。Redis 仍为 7.4.11，只使用签名 OpenSSL 3.3.7-r2 安全补丁库，用户为999:1000。源项目任何配置/数据库不会因为构建而被打开或启动。

需要重建 Redis 支持镜像时，在本独立源码包执行 `python scripts/build_support_images.py --kinds redis,caddy --architectures amd64,arm64`。工具只从冻结的官方 Redis 基础摘要构建，签名运行库版本严格指定，不需要兑换站文件。

镜像漏洞扫描使用 `scripts/scan_image.py`，可配官方 Trivy 离线库和 `--export-rootfs` 扫描不曾启动的临时容器文件系统。原始扫描、前端依赖告警与安全补丁验收分别保留；镜像系统扫描不代替前端补丁检查。离线镜像传输使用 `scripts/export_images.py`，只导出匹配构建证据的本地镜像并附 SHA-256 清单。

Caddy 支持镜像以固定官方2.11.7为基础，仅移除文件绑定能力并准备 `/data` 与 `/config` 属主 UID/GID10001。新卷继承属主，恢复已有证书/配置卷须先将卷内容设为该用户可写。compose 在独立容器网络命名空间设置低端口 sysctl，全部能力仍为零，宿主机 sysctl 不变。两个架构均已真实验证本地受信TLS、80/443、证书持久化和重启；不会在验收中申请外部 ACME。运行/扫描证据入口为 `deploy/VALIDATION.md`。
