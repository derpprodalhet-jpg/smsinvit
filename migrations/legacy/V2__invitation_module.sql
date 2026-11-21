-- Invitation work deliberately uses its own queue: durable_jobs retries are unsafe for this flow.
CREATE TABLE invitation_settings (
    id TINYINT NOT NULL PRIMARY KEY,
    settings_ciphertext MEDIUMTEXT NOT NULL,
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    CONSTRAINT invitation_settings_singleton CHECK (id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE invitation_mail_accounts (
    id CHAR(36) NOT NULL PRIMARY KEY,
    email VARCHAR(254) NOT NULL,
    email_fingerprint CHAR(64) NOT NULL,
    kind VARCHAR(20) NOT NULL,
    credentials_ciphertext MEDIUMTEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'AVAILABLE',
    note VARCHAR(1000) NOT NULL DEFAULT '',
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    UNIQUE KEY invitation_mail_email_unique (email_fingerprint),
    KEY invitation_mail_available_idx (status, created_at),
    CONSTRAINT invitation_mail_kind CHECK (kind IN ('OUTLOOK','IMAP','CF_TEMP')),
    CONSTRAINT invitation_mail_status CHECK (status IN ('AVAILABLE','CLAIMED','DONE','DISABLED'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE invitation_proxies (
    id CHAR(36) NOT NULL PRIMARY KEY,
    url_ciphertext TEXT NOT NULL,
    url_fingerprint CHAR(64) NOT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    note VARCHAR(1000) NOT NULL DEFAULT '',
    last_status VARCHAR(100) NULL,
    last_tested_at DATETIME(3) NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    UNIQUE KEY invitation_proxy_url_unique (url_fingerprint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE invitation_redeem_keys (
    id CHAR(36) NOT NULL PRIMARY KEY,
    inventory_item_id CHAR(36) NOT NULL,
    sku_id CHAR(36) NOT NULL,
    code_fingerprint CHAR(64) NOT NULL,
    claimed_mail_account_id CHAR(36) NULL,
    claimed_email_fingerprint CHAR(64) NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'UNUSED',
    attributes_snapshot_json JSON NOT NULL,
    expires_at DATETIME(3) NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    UNIQUE KEY invitation_key_inventory_unique (inventory_item_id),
    UNIQUE KEY invitation_key_code_unique (code_fingerprint),
    UNIQUE KEY invitation_key_mail_claim_unique (claimed_mail_account_id),
    UNIQUE KEY invitation_key_email_claim_unique (claimed_email_fingerprint),
    CONSTRAINT invitation_key_inventory_fk FOREIGN KEY (inventory_item_id) REFERENCES inventory_items(id),
    CONSTRAINT invitation_key_sku_fk FOREIGN KEY (sku_id) REFERENCES skus(id),
    CONSTRAINT invitation_key_status CHECK (status IN ('UNUSED','CLAIMED','CONSUMED','DISABLED','EXPIRED'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE invitation_runs (
    id CHAR(36) NOT NULL PRIMARY KEY,
    redeem_key_id CHAR(36) NOT NULL,
    mail_account_id CHAR(36) NOT NULL,
    email VARCHAR(254) NOT NULL,
    email_fingerprint CHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PREVIEWED',
    is_current TINYINT(1) NOT NULL DEFAULT 1,
    invitation_status VARCHAR(20) NOT NULL DEFAULT 'unknown',
    execution_ok TINYINT(1) NOT NULL DEFAULT 0,
    retry_safe TINYINT(1) NOT NULL DEFAULT 0,
    progress_percent INT NOT NULL DEFAULT 0,
    attempt INT NOT NULL DEFAULT 1,
    account_credentials_ciphertext MEDIUMTEXT NULL,
    execution_settings_ciphertext MEDIUMTEXT NULL,
    proxy_id CHAR(36) NULL,
    status_detail VARCHAR(1000) NOT NULL DEFAULT '',
    verification_attempt INT NULL,
    inviter_status VARCHAR(20) NULL,
    reward_received TINYINT(1) NOT NULL DEFAULT 0,
    verification_evidence_ciphertext MEDIUMTEXT NULL,
    verified_by CHAR(36) NULL,
    verified_at DATETIME(3) NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    queued_at DATETIME(3) NULL,
    started_at DATETIME(3) NULL,
    finished_at DATETIME(3) NULL,
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    active_key_id CHAR(36) GENERATED ALWAYS AS
      (CASE WHEN status IN ('QUEUED','RUNNING') THEN redeem_key_id ELSE NULL END) STORED,
    current_key_id CHAR(36) GENERATED ALWAYS AS
      (CASE WHEN is_current=1 THEN redeem_key_id ELSE NULL END) STORED,
    UNIQUE KEY invitation_run_current_key_unique (current_key_id),
    UNIQUE KEY invitation_run_active_key_unique (active_key_id),
    KEY invitation_run_queue_idx (status,queued_at),
    KEY invitation_run_created_idx (created_at),
    CONSTRAINT invitation_run_key_fk FOREIGN KEY (redeem_key_id) REFERENCES invitation_redeem_keys(id),
    CONSTRAINT invitation_run_mail_fk FOREIGN KEY (mail_account_id) REFERENCES invitation_mail_accounts(id),
    CONSTRAINT invitation_run_progress CHECK (progress_percent BETWEEN 0 AND 100),
    CONSTRAINT invitation_run_attempt CHECK (attempt >= 1),
    CONSTRAINT invitation_run_status CHECK (status IN ('PREVIEWED','QUEUED','RUNNING','REVIEW','SUCCEEDED','FAILED')),
    CONSTRAINT invitation_run_invitation_status CHECK (invitation_status IN ('unknown','pending','confirmed','rejected')),
    CONSTRAINT invitation_run_success_evidence CHECK (
      (status <> 'SUCCEEDED' AND invitation_status <> 'confirmed') OR
      (status = 'SUCCEEDED' AND invitation_status = 'confirmed' AND inviter_status = 'accepted'
       AND inviter_status IS NOT NULL AND verification_attempt IS NOT NULL
       AND reward_received = 1 AND verification_evidence_ciphertext IS NOT NULL
       AND verified_by IS NOT NULL AND verified_at IS NOT NULL AND verification_attempt = attempt))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE invitation_verifications (
    id CHAR(36) NOT NULL PRIMARY KEY,
    run_id CHAR(36) NOT NULL,
    attempt INT NOT NULL,
    inviter_status VARCHAR(20) NOT NULL,
    reward_received TINYINT(1) NOT NULL,
    evidence_ciphertext MEDIUMTEXT NULL,
    actor_id CHAR(36) NOT NULL,
    verified_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    KEY invitation_verification_run_idx (run_id,attempt,verified_at),
    CONSTRAINT invitation_verification_run_fk FOREIGN KEY (run_id) REFERENCES invitation_runs(id),
    CONSTRAINT invitation_verification_status CHECK (inviter_status IN ('accepted','pending','rejected','unknown'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE invitation_run_events (
    id CHAR(36) NOT NULL PRIMARY KEY,
    run_id CHAR(36) NOT NULL,
    attempt INT NOT NULL,
    stage VARCHAR(100) NOT NULL,
    message_ciphertext MEDIUMTEXT NOT NULL,
    progress_percent INT NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    KEY invitation_event_run_idx (run_id,created_at),
    CONSTRAINT invitation_event_run_fk FOREIGN KEY (run_id) REFERENCES invitation_runs(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE invitation_worker_lock (id TINYINT NOT NULL PRIMARY KEY, touched_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3));
INSERT INTO invitation_worker_lock(id) VALUES(1);

INSERT INTO permissions(id,resource,action,code,is_sensitive) VALUES
('eadf044e-0001-4000-8000-000000000001','invitation','read','invitation.read',0),
('eadf044e-0001-4000-8000-000000000002','invitation','manage','invitation.manage',0),
('eadf044e-0001-4000-8000-000000000003','invitation','verify','invitation.verify',0),
('eadf044e-0001-4000-8000-000000000004','invitation','credentials','invitation.credentials',1);
INSERT INTO role_permissions(id,role_id,permission_id,scope_type)
SELECT UUID(),r.id,p.id,'ALL' FROM roles r CROSS JOIN permissions p
WHERE r.code IN ('ADMIN','SUPER_ADMIN') AND p.resource='invitation';
