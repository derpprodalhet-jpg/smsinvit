-- Learned NANP area-code exclusions; never store the originating full phone.
CREATE TABLE sms_voip_area_exclusions (
    prefix VARCHAR(4) NOT NULL,
    line_type VARCHAR(32) NOT NULL,
    detection_count BIGINT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (prefix)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
