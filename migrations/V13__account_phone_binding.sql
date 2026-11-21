-- 账号池「是否接码」状态。
-- 注册阶段会 best-effort 顺手把手机验掉：验成功 → 之后 CDK 兑换不再命中 add-phone
-- （省掉一次租号 + 等码，实测 ~28s）；验失败**不影响**入池 —— 绑上 2FA 就算
-- GPT 注册成功，只是兑换时会走到 add-phone 阶段。
--   phone_bound_ok: -1 = 未尝试/未接码，1 = 已接码，0 = 接码失败
ALTER TABLE invitation_v2_outlook_accounts
  ADD COLUMN `phone_bound_at` DOUBLE,
  ADD COLUMN `phone_bound_ok` BIGINT NOT NULL DEFAULT -1;
