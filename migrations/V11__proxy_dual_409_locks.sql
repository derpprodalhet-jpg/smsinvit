-- 代理池双锁：GPT 注册锁（register）与 Codex 激活锁（codex）各自独立的 409 生命周期。
--
-- 背景：409 生命周期原本只有一套计数/冻结/永久标记，两种用途互相污染 ——
-- 注册阶段（warmup 拿不到 oai-did 必 409）冻掉的代理，会把还能用来做
-- Codex 激活的代理一起判死；反之亦然。现在两个维度完全独立：
--   连续 2 次 409 → 冻结 20 分钟；解冻后再 1 次 → 该维度永久不可用。
--
-- 回填策略：历史计数全部归入 register（它们来自 warmup 预检），codex 从 0 起。
-- 旧列保留不动（migrations/V7 声明了它们，schema 校验要求存在）。
ALTER TABLE invitation_v2_proxy_pool
  ADD COLUMN `consecutive_409_register` BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN `total_409_register` BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN `frozen_once_register` BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN `frozen_until_register` DOUBLE,
  ADD COLUMN `freeze_count_register` BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN `permanent_dead_register` BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN `last_409_at_register` DOUBLE;

ALTER TABLE invitation_v2_proxy_pool
  ADD COLUMN `consecutive_409_codex` BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN `total_409_codex` BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN `frozen_once_codex` BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN `frozen_until_codex` DOUBLE,
  ADD COLUMN `freeze_count_codex` BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN `permanent_dead_codex` BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN `last_409_at_codex` DOUBLE;

UPDATE invitation_v2_proxy_pool
   SET consecutive_409_register = consecutive_409,
       total_409_register = total_409,
       frozen_once_register = frozen_once,
       frozen_until_register = frozen_until,
       freeze_count_register = freeze_count,
       permanent_dead_register = permanent_dead,
       last_409_at_register = last_409_at;
