-- Import state is separate from live invitation execution. Original rows are encrypted,
-- including previous attempts that cannot enter the one-current-run-per-key table.
CREATE TABLE invitation_legacy_sources (
    id CHAR(36) NOT NULL PRIMARY KEY,
    source_fingerprint CHAR(64) NOT NULL,
    sku_id CHAR(36) NOT NULL,
    import_batch_id CHAR(36) NOT NULL,
    imported_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    UNIQUE KEY invitation_legacy_source_unique(source_fingerprint),
    CONSTRAINT invitation_legacy_source_sku_fk FOREIGN KEY(sku_id) REFERENCES skus(id),
    CONSTRAINT invitation_legacy_source_batch_fk FOREIGN KEY(import_batch_id) REFERENCES inventory_import_batches(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE invitation_legacy_import_map (
    source_id CHAR(36) NOT NULL,
    entity_kind VARCHAR(40) NOT NULL,
    old_id_fingerprint CHAR(64) NOT NULL,
    old_id_ciphertext MEDIUMTEXT NOT NULL,
    target_id CHAR(36) NULL,
    row_ciphertext MEDIUMTEXT NOT NULL,
    imported_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY(source_id,entity_kind,old_id_fingerprint),
    CONSTRAINT invitation_legacy_map_source_fk FOREIGN KEY(source_id) REFERENCES invitation_legacy_sources(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
