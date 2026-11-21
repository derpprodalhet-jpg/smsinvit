-- Keep one stable row per area, including removed entries. Concurrent add,
-- remove and VoIP detection then contend on one record rather than insert gaps.
ALTER TABLE sms_voip_area_exclusions ADD COLUMN active TINYINT NOT NULL DEFAULT 1;
