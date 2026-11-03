# Design decisions

- Member verdicts (L-spec-2012): after a red window each remaining member is verified alone on the window base (parallel, bounded by `member_verify_parallel`), recorded as a durable `member-verdict`, and a green verdict on the exact git tree is reused for `verdict_fresh_hours` by members, candidates and `base_pg`; undetermined is never green and never reusable.

branch-refresh: refresh completed single-commit branches in detached worktrees before builder recovery; preserve packet authority, author/message and existing judgments, require Verification and project guards, refuse migrations/fingerprints, and bound attempts per UTC day.
