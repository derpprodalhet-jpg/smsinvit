-- Preserve historical reward-verified success without promoting old REVIEW executions.
ALTER TABLE invitation_runs
    ADD COLUMN completion_basis VARCHAR(30) NULL,
    ADD COLUMN cli_completed TINYINT(1) NOT NULL DEFAULT 0;

UPDATE invitation_runs
SET completion_basis='LEGACY_REWARD_VERIFIED'
WHERE status='SUCCEEDED';

-- Flow completion and inviter-side reward evidence are independent conclusions.
ALTER TABLE invitation_runs
    DROP CHECK invitation_run_success_evidence,
    ADD CONSTRAINT invitation_run_completion_basis CHECK (
      (status <> 'SUCCEEDED' AND completion_basis IS NULL) OR
      (status = 'SUCCEEDED' AND completion_basis IS NOT NULL AND
       (completion_basis = 'LEGACY_REWARD_VERIFIED' OR
        (completion_basis = 'INVITEE_FLOW_CLI' AND execution_ok = 1 AND cli_completed = 1)))),
    ADD CONSTRAINT invitation_run_cli_completed CHECK (cli_completed IN (0,1)),
    ADD CONSTRAINT invitation_run_reward_evidence CHECK (
      invitation_status <> 'confirmed' OR
      (inviter_status IS NOT NULL AND inviter_status = 'accepted' AND reward_received = 1
       AND verification_attempt IS NOT NULL AND verification_attempt = attempt
       AND verification_evidence_ciphertext IS NOT NULL
       AND verified_by IS NOT NULL AND verified_at IS NOT NULL));
