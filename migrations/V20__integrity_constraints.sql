-- 数据完整性约束（保守子集）。
-- 只加「现有历史数据不可能违反」的约束；无法在迁移里安全判定的项（各表 status 的取值集合、
-- invitation_v2_platform_keys.expires_at 的 NULL 语义）留给后续单独处理，理由见
-- docs/validation/2026-10-10-batch-cardkey-stop-zh-ui.md §19.4。

-- 金额类必须非负：负数会直接污染成本统计（成本是 SUM(activation_cost)，
-- 一个 -500 就能把整列抹成负数）。NULL 仍然允许（历史行未记录金额）。
ALTER TABLE sms_sessions
    ADD CONSTRAINT sms_sessions_activation_cost_nonneg CHECK (activation_cost IS NULL OR activation_cost >= 0);

ALTER TABLE invitation_v2_sms_orders
    ADD CONSTRAINT invitation_v2_sms_orders_cost_nonneg CHECK (activation_cost IS NULL OR activation_cost >= 0);
