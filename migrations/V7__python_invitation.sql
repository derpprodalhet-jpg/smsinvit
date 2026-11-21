-- Invitation private tables. Source SQLite dialect is converted; all runtime storage is MySQL.

CREATE TABLE IF NOT EXISTS invitation_v2_outlook_accounts (
  `email` VARCHAR(320),
  `password` LONGTEXT,
  `client_id` LONGTEXT,
  `refresh_token` LONGTEXT,
  `relay_url` LONGTEXT,
  `kind` VARCHAR(160) NOT NULL DEFAULT 'outlook',
  `status` VARCHAR(160) NOT NULL DEFAULT 'available',
  `imported_at` DOUBLE,
  `claimed_at` DOUBLE,
  `finished_at` DOUBLE,
  `fail_reason` LONGTEXT,
  `proxy` LONGTEXT,
  `cdk_code` VARCHAR(72),
  `cdk_bound_at` DOUBLE,
  `mail_kind` VARCHAR(160),
  PRIMARY KEY (`email`),
  KEY `idx_outlook_status` (`status`),
  UNIQUE KEY `invitation_v2_one_mailbox_per_code` (`cdk_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_settings (
  `key` VARCHAR(160),
  `value` LONGTEXT,
  PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_registered (
  `email` VARCHAR(320),
  `password` LONGTEXT,
  `access_token` LONGTEXT,
  `session_token` LONGTEXT,
  `refresh_token` LONGTEXT,
  `id_token` LONGTEXT,
  `device_id` LONGTEXT,
  `csrf_token` LONGTEXT,
  `cookie_header` LONGTEXT,
  `totp_secret` LONGTEXT,
  `totp_factor_id` LONGTEXT,
  `extra_json` LONGTEXT,
  `created_at` DOUBLE,
  `proxy` LONGTEXT,
  `source` LONGTEXT,
  PRIMARY KEY (`email`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_runs (
  `run_id` VARCHAR(160),
  `email` VARCHAR(320),
  `status` VARCHAR(160),
  `started_at` DOUBLE,
  `finished_at` DOUBLE,
  `log_path` LONGTEXT,
  `error` LONGTEXT,
  `error_category` VARCHAR(160),
  `kind` VARCHAR(160),
  `owner_pid` BIGINT,
  PRIMARY KEY (`run_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_sms_refund_tasks (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `platform` VARCHAR(160) NOT NULL,
  `activation_id` VARCHAR(160) NOT NULL,
  `reason` LONGTEXT,
  `attempts` BIGINT NOT NULL DEFAULT 0,
  `max_attempts` BIGINT NOT NULL DEFAULT 3,
  `status` VARCHAR(160) NOT NULL DEFAULT 'pending',
  `next_at` DOUBLE,
  `last_error` LONGTEXT,
  `created_at` DOUBLE,
  `updated_at` DOUBLE,
  PRIMARY KEY (`id`),
  UNIQUE KEY `sqlite_autoindex_sms_refund_tasks_1` (`platform`,`activation_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_proxy_pool (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `url` VARCHAR(72) NOT NULL,
  `label` LONGTEXT,
  `enabled` BIGINT NOT NULL DEFAULT 1,
  `use_count` BIGINT NOT NULL DEFAULT 0,
  `success_count` BIGINT NOT NULL DEFAULT 0,
  `fail_count` BIGINT NOT NULL DEFAULT 0,
  `consecutive_409` BIGINT NOT NULL DEFAULT 0,
  `total_409` BIGINT NOT NULL DEFAULT 0,
  `frozen_once` BIGINT NOT NULL DEFAULT 0,
  `frozen_until` DOUBLE,
  `freeze_count` BIGINT NOT NULL DEFAULT 0,
  `permanent_dead` BIGINT NOT NULL DEFAULT 0,
  `last_used_at` DOUBLE,
  `last_409_at` DOUBLE,
  `last_error` LONGTEXT,
  `added_at` DOUBLE,
  `updated_at` DOUBLE,
  PRIMARY KEY (`id`),
  UNIQUE KEY `sqlite_autoindex_proxy_pool_1` (`url`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_proxy_events (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `proxy_id` BIGINT,
  `url` VARCHAR(72),
  `event` VARCHAR(160),
  `detail` LONGTEXT,
  `created_at` DOUBLE,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_sms_prices (
  `platform` VARCHAR(160) NOT NULL,
  `service` VARCHAR(160) NOT NULL,
  `country` VARCHAR(160) NOT NULL,
  `price` DOUBLE,
  `count` BIGINT,
  `physical_count` BIGINT,
  `min_price` DOUBLE,
  `raw_json` LONGTEXT,
  `updated_at` DOUBLE,
  `ok` BIGINT NOT NULL DEFAULT 1,
  `error` LONGTEXT,
  PRIMARY KEY (`platform`,`service`,`country`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_sms_stats (
  `platform` VARCHAR(160) NOT NULL,
  `service` VARCHAR(160) NOT NULL,
  `country` VARCHAR(160) NOT NULL,
  `attempts` BIGINT NOT NULL DEFAULT 0,
  `ok` BIGINT NOT NULL DEFAULT 0,
  `fail` BIGINT NOT NULL DEFAULT 0,
  `last_used_at` DOUBLE,
  PRIMARY KEY (`platform`,`service`,`country`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_sms_price_state (
  `platform` VARCHAR(160),
  `last_poll_at` DOUBLE,
  `last_ok_at` DOUBLE,
  `last_error` LONGTEXT,
  `balance` DOUBLE,
  `best_country` LONGTEXT,
  `best_price` DOUBLE,
  `best_score` DOUBLE,
  `best_success_rate` DOUBLE,
  `best_at` DOUBLE,
  PRIMARY KEY (`platform`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_cdk_codes (
  `code` VARCHAR(72),
  `batch` VARCHAR(160),
  `note` LONGTEXT,
  `status` VARCHAR(160) NOT NULL DEFAULT 'unused',
  `email` VARCHAR(320),
  `mail_kind` VARCHAR(160),
  `inviter_email` LONGTEXT,
  `inviter_subject` LONGTEXT,
  `invite_link` LONGTEXT,
  `invite_received_at` DOUBLE,
  `invite_sent_at` DOUBLE,
  `invite_processed_at` DOUBLE,
  `attempts` BIGINT NOT NULL DEFAULT 0,
  `fail_count` BIGINT NOT NULL DEFAULT 0,
  `proxy_used` LONGTEXT,
  `created_at` DOUBLE,
  `redeemed_at` DOUBLE,
  `finished_at` DOUBLE,
  `result_json` LONGTEXT,
  `error` LONGTEXT,
  PRIMARY KEY (`code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_cdk_jobs (
  `job_id` VARCHAR(160),
  `code` VARCHAR(72) NOT NULL,
  `email` VARCHAR(320),
  `attempt` BIGINT NOT NULL DEFAULT 1,
  `status` VARCHAR(160) NOT NULL,
  `stage` VARCHAR(160),
  `progress` BIGINT NOT NULL DEFAULT 0,
  `proxy` LONGTEXT,
  `error` LONGTEXT,
  `created_at` DOUBLE,
  `started_at` DOUBLE,
  `finished_at` DOUBLE,
  PRIMARY KEY (`job_id`),
  `active_code` VARCHAR(72) GENERATED ALWAYS AS (CASE WHEN status IN ('queued','running') THEN code ELSE NULL END) STORED,
  UNIQUE KEY `invitation_v2_one_active_job` (`active_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_cdk_events (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `code` VARCHAR(72),
  `job_id` VARCHAR(160),
  `event` VARCHAR(160),
  `detail` LONGTEXT,
  `created_at` DOUBLE,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_codex_auth (
  `email` VARCHAR(320),
  `account_id` LONGTEXT,
  `access_token` LONGTEXT,
  `refresh_token` LONGTEXT,
  `id_token` LONGTEXT,
  `auth_json` LONGTEXT,
  `web_access_token` LONGTEXT,
  `session_token` LONGTEXT,
  `last_refresh` LONGTEXT,
  `invite_processed_at` DOUBLE,
  `invite_link` LONGTEXT,
  `cli_completed_at` DOUBLE,
  `cli_completed_account_id` LONGTEXT,
  `updated_at` DOUBLE,
  PRIMARY KEY (`email`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_secret_values (
 fingerprint CHAR(64) PRIMARY KEY, ciphertext LONGTEXT NOT NULL, plaintext_length INT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS invitation_v2_platform_keys (
 inventory_item_id CHAR(36) PRIMARY KEY, sku_id CHAR(36) NOT NULL, code_id VARCHAR(72) NOT NULL UNIQUE,
 code_fingerprint CHAR(64) NOT NULL UNIQUE, expires_at DATETIME(3), snapshot_json JSON NOT NULL,
 FOREIGN KEY (inventory_item_id) REFERENCES inventory_items(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS invitation_v2_sessions (
 id CHAR(36) PRIMARY KEY, code_id VARCHAR(72) NOT NULL UNIQUE, origin VARCHAR(32) NOT NULL DEFAULT 'native',
 created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS invitation_v2_import_map (
 source_name VARCHAR(80) NOT NULL, source_table VARCHAR(80) NOT NULL, source_id VARCHAR(320) NOT NULL,
 target_id VARCHAR(320) NOT NULL, source_checksum CHAR(64) NOT NULL,
 PRIMARY KEY (source_name,source_table,source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS invitation_v2_task_holds (
 job_id VARCHAR(160) PRIMARY KEY, original_status VARCHAR(32) NOT NULL, reason VARCHAR(160) NOT NULL,
 created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3), resolved_at DATETIME(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS invitation_v2_legacy_documents (
 source_table VARCHAR(80) NOT NULL, source_id VARCHAR(320) NOT NULL, ciphertext LONGTEXT NOT NULL,
 PRIMARY KEY(source_table,source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
