-- Durable permits fence old executors even after their advisory lock connection
-- is lost. Occupied slots are never reclaimed by a timer or application restart.
CREATE TABLE IF NOT EXISTS invitation_v2_execution_permits (
  slot_id TINYINT UNSIGNED NOT NULL PRIMARY KEY,
  owner_token CHAR(36) NULL,
  instance_id CHAR(36) NULL,
  email VARCHAR(320) NULL,
  task_id VARCHAR(160) NULL,
  acquired_at DATETIME(3) NULL,
  UNIQUE KEY invitation_v2_permit_email_unique (email),
  UNIQUE KEY invitation_v2_permit_owner_unique (owner_token)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
INSERT IGNORE INTO invitation_v2_execution_permits(slot_id) VALUES
(0),(1),(2),(3),(4),(5),(6),(7),(8),(9),(10),(11),(12),(13),(14),(15),(16),
(17),(18),(19),(20),(21),(22),(23),(24),(25),(26),(27),(28),(29),(30),(31),(32);
