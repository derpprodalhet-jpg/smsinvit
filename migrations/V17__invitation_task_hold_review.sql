-- Manual review closure for interrupted invitation tasks.
--
-- A restart inserts one row per queued/running job into invitation_v2_task_holds
-- to mark the outcome as uncertain. Until now nothing in production ever wrote
-- resolved_at, so an uncertain task blocked its card and its mailbox permanently
-- and the only way out was editing the database by hand.
--
-- These columns record who closed the review, what they concluded, the operator's
-- own explanation and whether they confirmed that every executor had stopped. The
-- explanation is stored as an encrypted marker (never plain text), and the audit
-- stream keeps only a digest. A resolution never rewrites an uncertain outcome as
-- success: it only releases the block so an operator can decide the outcome with
-- the existing card tools.
ALTER TABLE invitation_v2_task_holds
  ADD COLUMN resolved_by VARCHAR(64) NULL,
  ADD COLUMN resolution VARCHAR(32) NULL,
  ADD COLUMN note VARCHAR(160) NULL,
  ADD COLUMN confirmed_executors_stopped TINYINT(1) NULL;
