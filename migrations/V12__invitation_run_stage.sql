-- 运行记录自带阶段：账号工厂补号没有卡密作业，admin 的 /api/runs 只能从
-- cdk_jobs 取 stage/progress，于是补号那条永远是 0%，跑完直接跳 100%。
-- 这里让 runs 自己记阶段（进度% 由 USER_STAGE_PCT 在读的时候算，避免两处维护阶梯）。
ALTER TABLE invitation_v2_runs
  ADD COLUMN `stage` VARCHAR(160);

-- 回填：已完成的运行直接标成完成态
UPDATE invitation_v2_runs SET stage='done' WHERE status='done' AND (stage IS NULL OR stage='');
