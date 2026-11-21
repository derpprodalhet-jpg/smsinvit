CREATE TABLE IF NOT EXISTS invitation_v2_redemption_attempts (
 attempt_id CHAR(36) PRIMARY KEY,
 recovery_fingerprint CHAR(64) NOT NULL,
 codes_fingerprint CHAR(64) NOT NULL,
 status VARCHAR(32) NOT NULL,
 owner_token CHAR(36),
 created_at DOUBLE NOT NULL,
 updated_at DOUBLE NOT NULL,
 result_json JSON,
 error_code VARCHAR(80),
 error_message VARCHAR(500)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS invitation_v2_redemption_attempt_cards (
 attempt_id CHAR(36) NOT NULL,
 code_fingerprint CHAR(64) NOT NULL,
 PRIMARY KEY (attempt_id,code_fingerprint),
 KEY idx_invitation_redemption_card (code_fingerprint),
 CONSTRAINT fk_invitation_redemption_attempt FOREIGN KEY (attempt_id)
   REFERENCES invitation_v2_redemption_attempts(attempt_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
