-- 账号是否**真的**走过代理：让"直连"和"没记录"不再混为一谈。
--   used_proxy: -1 = 未记录（历史数据）/ 1 = 有代理 / 0 = 直连
ALTER TABLE invitation_v2_outlook_accounts
  ADD COLUMN `used_proxy` BIGINT NOT NULL DEFAULT -1;
