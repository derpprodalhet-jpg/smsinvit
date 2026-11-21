-- 后台登录爆破防护：同一 IP 连续 5 次密码错误，封禁该 IP 24 小时。
--
-- 背景：既有的两条登录限流都挡不住定向爆破。
--   * `admin-login-id` 是 5 次 / 15 分钟，但按「账号标识」计数：攻击者每轮换一个用户名，
--     或者干脆对着不存在的账号撞，计数永远停在 1。
--   * `admin-login-ip` 是 50 次 / 15 分钟，对单个 IP 太宽，15 分钟足够跑完一轮常用口令。
-- 这里把「密码错误」按来源 IP 单独计数，达阈值即封 IP，作用于整个 `/api/v1/admin/*`。
--
-- 设计取舍：
--   * 只数密码错误（`AUTH_INVALID_CREDENTIALS`，账号不存在返回同款报错，一并计入），
--     验证码错误与 TOTP 错误不计入——把「验证码输错」算成爆破会误伤正常管理员。
--   * 计数窗口与封禁时长同为 24 小时：封禁到期时计数窗口也已过期，下一次失败从 1 开始，
--     不会出现「刚解封又失手一次就被再封」。
--   * 登录成功即删除该 IP 整行，语义是「连续失败」，不保留很久以前的失败记录。
--   * `ip_address` 存明文：后台封禁页需要人可读的地址，`admin_sessions.ip_address` 已是同样
--     处理；`ip_hash` 只作主键，避免把可变长度的 IPv6 字面量拼进索引键。
--   * 封禁是「读多写少」的热路径（每个后台请求都要判一次），主键查询即完成，无需额外索引。
CREATE TABLE admin_ip_bans (
    ip_hash CHAR(64) NOT NULL,
    ip_address VARCHAR(64) NOT NULL,
    failure_count INT NOT NULL DEFAULT 0,
    first_failure_at DATETIME(3) NOT NULL,
    last_failure_at DATETIME(3) NOT NULL,
    last_identifier VARCHAR(255),
    banned_until DATETIME(3),
    ban_reason VARCHAR(64),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (ip_hash),
    KEY admin_ip_bans_banned_until_idx (banned_until),
    KEY admin_ip_bans_last_failure_idx (last_failure_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 专用权限点：查看并解除 IP 封禁。V1 的 `roles CROSS JOIN permissions` 种子运行在本迁移
-- 之前，新建的权限点不会被它补发，因此这里显式授权，保持「系统内建角色默认拥有全部权限点」
-- 的既有语义（`security.ipban` 只影响能否看/解封，不改变封禁本身的自动生效）。
INSERT INTO permissions(id, resource, action, code, description, is_sensitive)
VALUES (UUID(), 'security', 'ipban', 'security.ipban', '查看并解除后台登录 IP 封禁', 0);

INSERT INTO role_permissions(id, role_id, permission_id, scope_type)
SELECT UUID(), r.id, p.id, 'ALL' FROM roles r CROSS JOIN permissions p WHERE p.code = 'security.ipban';
