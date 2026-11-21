-- Cost statistics need the amount actually paid per codex rental and its unit.
-- Every new column is nullable: rows written before this migration keep NULL and
-- are reported as unpriced instead of being guessed or back-filled.
ALTER TABLE invitation_v2_sms_orders
    ADD COLUMN activation_cost DECIMAL(12,4) NULL,
    ADD COLUMN currency VARCHAR(8) NULL;

-- EXPLAIN on the populated release schema showed the cumulative invitation
-- redemption count (COUNT(*) ... WHERE redeemed_at IS NOT NULL) and every
-- business-day window doing a full table scan (type=ALL, 417 rows): this table
-- only has its varchar primary key, unlike the sms key table whose existing
-- status indexes are used for cumulative counts. Both indexes are added here;
-- measured after the change the same queries use range access.
ALTER TABLE invitation_v2_cdk_codes
    ADD KEY invitation_v2_cdk_codes_created_idx (created_at),
    ADD KEY invitation_v2_cdk_codes_redeemed_idx (redeemed_at);
