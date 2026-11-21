CREATE TABLE IF NOT EXISTS invitation_v2_sms_budgets (
  run_id VARCHAR(72) NOT NULL PRIMARY KEY,
  reserved_count INT NOT NULL DEFAULT 0,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_sms_orders (
  rental_id CHAR(36) NOT NULL PRIMARY KEY,
  run_id VARCHAR(72) NOT NULL,
  platform VARCHAR(32) NOT NULL,
  action VARCHAR(32) NOT NULL,
  status VARCHAR(32) NOT NULL,
  activation_id VARCHAR(72),
  phone_fingerprint CHAR(64),
  error LONGTEXT,
  attempts INT NOT NULL DEFAULT 0,
  lease_token CHAR(36),
  next_at DOUBLE,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  UNIQUE KEY invitation_v2_sms_order_activation (platform, activation_id),
  KEY invitation_v2_sms_order_run (run_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_sms_phone_uses (
  phone_fingerprint CHAR(64) NOT NULL PRIMARY KEY,
  first_run_id VARCHAR(72),
  first_used_at DOUBLE NOT NULL,
  evidence_source VARCHAR(160) NOT NULL,
  created_at DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_manual_run_links (
  run_id VARCHAR(72) NOT NULL PRIMARY KEY,
  code_id VARCHAR(72),
  completed_job_id VARCHAR(72),
  created_at DOUBLE NOT NULL,
  sync_status VARCHAR(32) NOT NULL DEFAULT 'pending',
  sync_error LONGTEXT,
  KEY invitation_v2_manual_run_code (code_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
