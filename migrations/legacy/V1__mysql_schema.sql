-- Independent SmsInvit redemption database. Never apply to the source SmsInvit database.
-- Fresh installs only; merchant/customer tables and triggers deliberately omitted.


CREATE TABLE admin_users (
    id CHAR(36) NOT NULL,
    username VARCHAR(50) NOT NULL,
    email VARCHAR(255) NOT NULL,
    password_hash TEXT NOT NULL,
    display_name VARCHAR(100),
    status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    totp_secret_encrypted TEXT,
    totp_enabled_at DATETIME(3),
    totp_backup_codes_hash TEXT,
    last_login_at DATETIME(3),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    deleted_at DATETIME(3),
    PRIMARY KEY (id),
    UNIQUE KEY admin_users_username_key (username),
    UNIQUE KEY admin_users_email_key (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE admin_sessions (
    id CHAR(36) NOT NULL,
    admin_user_id CHAR(36) NOT NULL,
    refresh_token_hash TEXT NOT NULL,
    expires_at DATETIME(3) NOT NULL,
    revoked_at DATETIME(3),
    ip_address VARCHAR(45),
    user_agent TEXT,
    last_used_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    KEY admin_sessions_admin_user_id_idx (admin_user_id),
    KEY admin_sessions_refresh_token_hash_idx (refresh_token_hash(64)),
    KEY admin_sessions_expires_at_idx (expires_at),
    CONSTRAINT admin_sessions_admin_user_id_fkey FOREIGN KEY (admin_user_id) REFERENCES admin_users (id) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE roles (
    id CHAR(36) NOT NULL,
    code VARCHAR(32) NOT NULL,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    is_system TINYINT(1) NOT NULL DEFAULT 0,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY roles_code_key (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE permissions (
    id CHAR(36) NOT NULL,
    resource VARCHAR(50) NOT NULL,
    action VARCHAR(50) NOT NULL,
    code VARCHAR(100) NOT NULL,
    description TEXT,
    is_sensitive TINYINT(1) NOT NULL DEFAULT 0,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY permissions_code_key (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE admin_user_roles (
    id CHAR(36) NOT NULL,
    admin_user_id CHAR(36) NOT NULL,
    role_id CHAR(36) NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    created_by CHAR(36),
    -- Single SUPER_ADMIN assignment enforced in application (SuperAdminSync); MySQL cannot
    -- combine FOREIGN KEY(role_id) with a generated column that references role_id.
    PRIMARY KEY (id),
    UNIQUE KEY admin_user_roles_unique (admin_user_id, role_id),
    KEY admin_user_roles_admin_user_id_idx (admin_user_id),
    CONSTRAINT admin_user_roles_admin_user_id_fkey FOREIGN KEY (admin_user_id) REFERENCES admin_users (id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT admin_user_roles_role_id_fkey FOREIGN KEY (role_id) REFERENCES roles (id) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE role_permissions (
    id CHAR(36) NOT NULL,
    role_id CHAR(36) NOT NULL,
    permission_id CHAR(36) NOT NULL,
    scope_type VARCHAR(50),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY role_permissions_role_id_permission_id_scope_type_key (role_id, permission_id, scope_type),
    CONSTRAINT role_permissions_role_id_fkey FOREIGN KEY (role_id) REFERENCES roles (id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT role_permissions_permission_id_fkey FOREIGN KEY (permission_id) REFERENCES permissions (id) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE auth_challenges (
    token_hash VARCHAR(64) NOT NULL,
    admin_user_id CHAR(36),
    purpose VARCHAR(40) NOT NULL,
    payload_encrypted TEXT,
    expires_at DATETIME(3) NOT NULL,
    consumed_at DATETIME(3),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (token_hash),
    KEY auth_challenges_expiry (expires_at),
    CONSTRAINT auth_challenges_admin_user_id_fkey FOREIGN KEY (admin_user_id) REFERENCES admin_users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE auth_rate_limits (
    key_hash VARCHAR(64) NOT NULL,
    attempts INT NOT NULL DEFAULT 1,
    resets_at DATETIME(3) NOT NULL,
    PRIMARY KEY (key_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE api_rate_limits (
    key_hash VARCHAR(64) NOT NULL,
    attempts INT NOT NULL DEFAULT 1,
    resets_at DATETIME(3) NOT NULL,
    PRIMARY KEY (key_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE skus (
 id CHAR(36) NOT NULL PRIMARY KEY,
 code VARCHAR(100) NOT NULL UNIQUE,
 name VARCHAR(200) NOT NULL,
 status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
 attributes_json JSON NOT NULL,
 created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
 updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
 deleted_at DATETIME(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE inventory_import_batches (
    id CHAR(36) NOT NULL,
    sku_id CHAR(36) NOT NULL,
    source TEXT NOT NULL,
    requested_count INT NOT NULL DEFAULT 0,
    success_count INT NOT NULL DEFAULT 0,
    duplicate_count INT NOT NULL DEFAULT 0,
    invalid_count INT NOT NULL DEFAULT 0,
    params_json JSON NOT NULL DEFAULT (CAST('{}' AS JSON)),
    attributes_snapshot_json JSON,
    note TEXT,
    actor_id CHAR(36),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    KEY inventory_import_batches_sku_created_idx (sku_id, created_at),
    CONSTRAINT inventory_import_batches_source_check CHECK (source IN ('PASTE', 'GENERATE')),
    CONSTRAINT inventory_import_batches_sku_id_fkey FOREIGN KEY (sku_id) REFERENCES skus (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE inventory_items (
    id CHAR(36) NOT NULL,
    sku_id CHAR(36) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'ISSUED',
    secret_ciphertext TEXT NOT NULL,
    secret_fingerprint VARCHAR(64) NOT NULL,
    issued_at DATETIME(3),
    import_batch_id CHAR(36),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY inventory_items_sku_id_secret_fingerprint_key (sku_id, secret_fingerprint),
    KEY inventory_items_sku_id_status_idx (sku_id, status),
    KEY inventory_items_import_batch_id_idx (import_batch_id),
    CONSTRAINT inventory_items_sku_id_fkey FOREIGN KEY (sku_id) REFERENCES skus (id) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT inventory_items_import_batch_id_fkey FOREIGN KEY (import_batch_id) REFERENCES inventory_import_batches (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE inventory_events (
    id CHAR(36) NOT NULL,
    inventory_item_id CHAR(36) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    actor_type VARCHAR(50) NOT NULL,
    actor_id VARCHAR(100),
    metadata_json JSON,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    KEY inventory_events_inventory_item_id_idx (inventory_item_id),
    CONSTRAINT inventory_events_inventory_item_id_fkey FOREIGN KEY (inventory_item_id) REFERENCES inventory_items (id) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE durable_jobs (
    id CHAR(36) NOT NULL,
    kind VARCHAR(100) NOT NULL,
    deduplication_key VARCHAR(200) NOT NULL,
    payload JSON NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 5,
    available_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    lease_token CHAR(36),
    locked_until DATETIME(3),
    last_error_code VARCHAR(100),
    request_id VARCHAR(100),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY durable_jobs_deduplication_key (deduplication_key),
    KEY durable_jobs_ready (available_at, created_at),
    KEY durable_jobs_expired_lease (locked_until),
    CONSTRAINT durable_jobs_status_check CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    CONSTRAINT durable_jobs_attempts_check CHECK (attempts >= 0),
    CONSTRAINT durable_jobs_max_attempts_check CHECK (max_attempts > 0),
    CONSTRAINT durable_jobs_lease_check CHECK (
        (status = 'RUNNING' AND lease_token IS NOT NULL AND locked_until IS NOT NULL)
        OR (status <> 'RUNNING' AND lease_token IS NULL AND locked_until IS NULL)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE worker_heartbeats (
    worker_id CHAR(36) NOT NULL,
    last_seen_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (worker_id),
    KEY worker_heartbeats_last_seen (last_seen_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE notification_channel_settings (
    `key` VARCHAR(100) NOT NULL,
    value_encrypted TEXT NOT NULL,
    updated_by CHAR(36),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (`key`),
    CONSTRAINT notification_channel_settings_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES admin_users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE notification_outbox (
    id CHAR(36) NOT NULL,
    event_key VARCHAR(200) NOT NULL,
    channel VARCHAR(20) NOT NULL,
    kind VARCHAR(40) NOT NULL,
    reference_id CHAR(36),
    payload_encrypted MEDIUMTEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    attempts INT NOT NULL DEFAULT 0,
    lease_token CHAR(36),
    locked_until DATETIME(3),
    expires_at DATETIME(3),
    last_error_code VARCHAR(100),
    remote_id VARCHAR(100),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY notification_outbox_event_key (event_key),
    KEY notification_outbox_pending (created_at, id),
    CONSTRAINT notification_outbox_channel_check CHECK (channel IN ('EMAIL', 'TELEGRAM')),
    CONSTRAINT notification_outbox_status_check CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'FAILED', 'UNCERTAIN', 'SKIPPED'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE settings (
    id CHAR(36) NOT NULL,
    `key` VARCHAR(100) NOT NULL,
    category VARCHAR(50) NOT NULL,
    value_json JSON NOT NULL,
    visibility VARCHAR(20) NOT NULL DEFAULT 'PRIVATE',
    is_secret TINYINT(1) NOT NULL DEFAULT 0,
    updated_by CHAR(36),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY settings_key_key (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE audit_logs (
    id CHAR(36) NOT NULL,
    actor_user_id CHAR(36),
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(50) NOT NULL,
    resource_id VARCHAR(100),
    before_json JSON,
    after_json JSON,
    ip_address VARCHAR(45),
    user_agent TEXT,
    request_id VARCHAR(100) NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    KEY audit_logs_actor_user_id_created_at_idx (actor_user_id, created_at),
    KEY audit_logs_resource_type_resource_id_idx (resource_type, resource_id),
    KEY audit_logs_action_created_at_idx (action, created_at),
    KEY audit_logs_request_id_idx (request_id),
    CONSTRAINT audit_logs_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES admin_users (id) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE sms_redeem_keys (
    id CHAR(36) NOT NULL,
    inventory_item_id CHAR(36) NOT NULL,
    sku_id CHAR(36) NOT NULL,
    code_fingerprint TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'UNUSED',
    sms_service TEXT NOT NULL,
    sms_country INT NOT NULL,
    sms_min_price DECIMAL(12, 4) NOT NULL,
    sms_max_price DECIMAL(12, 4) NOT NULL,
    sms_min_count INT NOT NULL DEFAULT 20,
    cdk_valid_days INT NOT NULL DEFAULT 15,
    issued_at DATETIME(3) NOT NULL,
    expires_at DATETIME(3) NOT NULL,
    used_at DATETIME(3),
    failed_at DATETIME(3),
    attempt_count INT NOT NULL DEFAULT 0,
    last_error_code TEXT,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY sms_redeem_keys_inventory_item_id (inventory_item_id),
    KEY sms_redeem_keys_status_expires_idx (status, expires_at),
    KEY sms_redeem_keys_fingerprint_idx (code_fingerprint(64)),
    CONSTRAINT sms_redeem_keys_price_check CHECK (sms_min_price >= 0 AND sms_max_price >= sms_min_price),
    CONSTRAINT sms_redeem_keys_min_count_check CHECK (sms_min_count >= 1),
    CONSTRAINT sms_redeem_keys_inventory_item_id_fkey FOREIGN KEY (inventory_item_id) REFERENCES inventory_items (id) ON DELETE CASCADE,
    CONSTRAINT sms_redeem_keys_sku_id_fkey FOREIGN KEY (sku_id) REFERENCES skus (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE sms_sessions (
    id CHAR(36) NOT NULL,
    redeem_key_id CHAR(36) NOT NULL,
    attempt INT NOT NULL,
    activation_id TEXT,
    phone TEXT,
    activation_cost DECIMAL(12, 4),
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING_NUMBER',
    sms_code TEXT,
    sms_text TEXT,
    next_sms_code TEXT,
    cancel_eligible_at DATETIME(3),
    number_expires_at DATETIME(3),
    completed_at DATETIME(3),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY sms_sessions_key_attempt_unique (redeem_key_id, attempt),
    KEY sms_sessions_activation_id_idx (activation_id(64)),
    KEY sms_sessions_status_updated_idx (status, updated_at),
    CONSTRAINT sms_sessions_redeem_key_id_fkey FOREIGN KEY (redeem_key_id) REFERENCES sms_redeem_keys (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE sms_events (
    id CHAR(36) NOT NULL,
    redeem_key_id CHAR(36),
    session_id CHAR(36),
    event_type TEXT NOT NULL,
    detail_json JSON NOT NULL DEFAULT (CAST('{}' AS JSON)),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    KEY sms_events_redeem_key_created_idx (redeem_key_id, created_at),
    KEY sms_events_session_created_idx (session_id, created_at),
    CONSTRAINT sms_events_redeem_key_id_fkey FOREIGN KEY (redeem_key_id) REFERENCES sms_redeem_keys (id) ON DELETE CASCADE,
    CONSTRAINT sms_events_session_id_fkey FOREIGN KEY (session_id) REFERENCES sms_sessions (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 同号多码：有序验证码列表 + 下一封监听窗 + 上游激活已结束标记
ALTER TABLE sms_sessions
    ADD COLUMN sms_codes JSON NULL AFTER sms_code,
    ADD COLUMN next_listen_until DATETIME(3) NULL AFTER next_sms_code,
    ADD COLUMN provider_finalized_at DATETIME(3) NULL AFTER next_listen_until;

UPDATE sms_sessions
SET sms_codes = JSON_ARRAY(sms_code)
WHERE sms_code IS NOT NULL AND sms_code <> '' AND sms_codes IS NULL;


-- SKU 排除号码前缀快照到 CDK
ALTER TABLE sms_redeem_keys
    ADD COLUMN sms_phone_exception TEXT NULL AFTER sms_min_count;


-- Persist the outbound acquisition claim and distinguish refundable acquisition
-- compensation from a user's consumed number attempt.
ALTER TABLE sms_sessions
    ADD COLUMN acquisition_started_at DATETIME(3) NULL,
    ADD COLUMN cancel_refund_attempt BOOLEAN NOT NULL DEFAULT FALSE;

-- Old PENDING_NUMBER rows may already have reached the provider. Never replay them.
UPDATE sms_sessions SET status='ACQUIRE_UNKNOWN', updated_at=NOW(3)
WHERE status='PENDING_NUMBER';


-- Reserve CDK fingerprints across SKUs within each redemption business. Existing
-- duplicate stock is retained and marked ambiguous instead of failing migration.
CREATE INDEX inventory_items_secret_fingerprint_idx ON inventory_items(secret_fingerprint);

CREATE TABLE redeem_cdk_registry (
    business_kind VARCHAR(32) NOT NULL,
    code_fingerprint VARCHAR(64) NOT NULL,
    owner_inventory_id CHAR(36),
    inventory_count INT NOT NULL DEFAULT 1,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (business_kind, code_fingerprint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;



INSERT INTO roles (id, code, name, description, is_system, created_at, updated_at) VALUES
('a0000000-0000-4000-8000-000000000001', 'ADMIN', '管理员', '后台运营管理', 1, CURRENT_TIMESTAMP(3), CURRENT_TIMESTAMP(3)),
('a0000000-0000-4000-8000-000000000002', 'SUPER_ADMIN', '超级管理员', '系统最高权限', 1, CURRENT_TIMESTAMP(3), CURRENT_TIMESTAMP(3));



INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('d07a4884-c498-52c1-868d-81c9a1872fc4','admin','access','admin.access',0);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('957f3e68-0114-50aa-a505-73d5e93c6abc','sku','read','sku.read',0);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('954eaf02-ff81-59e9-893f-74d9f91a58d1','sku','create','sku.create',0);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('d2f183dc-263a-5043-a8e2-c06785ca8a84','sku','update','sku.update',0);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('6b12a0bf-327f-5279-a7c3-191522e0c5a9','sku','delete','sku.delete',0);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('9b8cc403-ae94-5e4c-b569-45769c214007','inventory','read','inventory.read',0);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('00c937b6-92a3-5608-bdd1-3215e3f0255a','inventory','manage','inventory.manage',0);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('9a7d94b1-d193-54ee-8b15-bb7498bd25e4','inventory','invalidate','inventory.invalidate',0);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('75c10d3f-9a69-5458-bf02-3bdfe926e33b','inventory','export','inventory.export',1);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('d68dd0c2-62b5-51b1-8bd1-fa119b139162','settings','read','settings.read',0);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('03945e5a-433f-5ef1-a571-ebd8331aa336','settings','update','settings.update',0);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('c68e5f98-3dd1-528d-a71b-ae44952f66cb','user','read','user.read',0);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('01ceaa75-d34e-50e8-a9c0-43d0d442ee7d','user','manage','user.manage',0);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('e9b848c0-76f4-5ba8-985f-751412c0d1d3','user','ban','user.ban',0);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('ce0dee8b-9be8-5e97-9f01-983055e4cead','audit','read','audit.read',0);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('c17ec30e-c2c4-5e5d-bcaf-edbd7d031ff3','job','read','job.read',0);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES ('10c99520-57ce-548a-8c22-af6b14d52cd3','job','manage','job.manage',0);

INSERT INTO role_permissions(id,role_id,permission_id,scope_type) SELECT UUID(),r.id,p.id,'ALL' FROM roles r CROSS JOIN permissions p;

INSERT INTO settings (id, `key`, category, value_json, visibility, is_secret, created_at, updated_at) VALUES
(
    'c0000000-0000-4000-8000-000000000001',
    'sms.smsbower',
    'sms',
    CAST('{"baseUrl":"https://smsbower.page/stubs/handler_api.php","publicApiBase":"","webhookAllowedIps":"","minBalance":1,"webhookToken":null,"unavailableMessage":"服务器正在维护，请稍后再试"}' AS JSON),
    'PRIVATE',
    1,
    CURRENT_TIMESTAMP(3),
    CURRENT_TIMESTAMP(3)
);


