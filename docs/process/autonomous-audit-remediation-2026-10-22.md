# Autonomous debugging and creation audit: remediation

Date: 2026-10-22

Scope: [the follow-up audit](autonomous-debugging-creation-audit-2026-10-22.md), findings A01–A15, in the existing working tree. This work changes LoopForge, not the reconciliation application. No paid provider call, local inference trial, or live autonomy qualification was performed. Deterministic regressions establish control behavior; they do not establish that a model completes these workloads unattended.

## Implementation and regression mapping

| Finding | Implemented correction | Regression evidence |
| --- | --- | --- |
| A01 | Durable typed `ActionRejected` feedback, phase validation, safe specific path feedback and a frozen correction allowance. Denied proposals never execute. | `test_goal_coordinator.py`: wrong-phase correction and rejection exhaustion; `test_repository_investigation.py`: `test_a01_mistyped_scoped_path_can_be_corrected_without_executing_outside_scope`, existing scope and symlink denials. |
| A02 | Explicit baseline/candidate inspection. Candidate reads require the exact current hash; evidence retains its checkpoint and identity. | `test_a02_current_candidate_inspection_is_identity_bound`; baseline and unauthorized source tests. |
| A03 | Large worker results are immutable goal-owned artifacts with retrievable IDs; a failure summary remains inline. Context packing also archives large original options and operator evidence. | `test_a03_a04_worker_artifact_utf8_tail_is_losslessly_retrievable`; cross-goal artifact denial and original-evidence regressions. |
| A04 | UTF-8 byte cursors preserve complete long lines. Provider context carries a complete bounded page and continuation cursor. Distinct pages count as research progress; repeated covered pages do not. Query excerpts use synthetic ranges. | UTF-8 tail regression; fresh core paging/stagnation tests; `test_query_at_nonzero_offset_retains_valid_synthetic_evidence_range`. |
| A05 | Check the serialized Gemini request against 48 KB. Bound essential intake authority; compact optional content to retrievable original artifacts without dropping scope, goal, identity or failure. | `test_a05_quote_heavy_context_packs_to_retrievable_artifact`; fresh core `test_quote_heavy_acceptance_options_are_bounded_and_fully_retrievable`. |
| A06 | Passing measured baselines are valid for feature/creation work. Bugfix workloads still require measured behavioral failure. | `test_a06_passing_baseline_is_measured_for_creation`; existing environment/behavior reproduction denial tests. |
| A07 | Allowed edit paths and mandatory deliverables are separate. Assignment narrowing does not make every allowed file a required change. Independent acceptance remains frozen. | `test_assignment_narrows_scope_without_changing_authority`; campaign trusted acceptance regressions. |
| A08 | Versioned assignments preserve candidate snapshots and their manifest hashes. Approved environments may change only while idle. New milestones seed from preserved work; experiments seed from the last verified checkpoint or original measured baseline. | `test_a08_assignment_versions_preserve_candidate_and_scope`; fresh core enqueue/crash/seed tamper tests; fresh authority experiment checkpoint and narrowed maintenance tests. |
| A09 | Midtask questions use only frozen categories. Optional expiry uses its frozen fallback; permission silence grants nothing. Conflicting late answers pause work and can prepare an unstarted linked successor from the exact candidate, retaining the original parent budget. Repeating the handoff retains child identity and answers. | `test_a09_midtask_question_category_and_fallback_cannot_grant_authority`; fresh authority first-verification expiry, shared ancestry and successor regressions; existing goal review conflict/control tests. |
| A10 | Explicit trusted intake can authorize ordinary tests, manifests and hidden project configuration. Trusted acceptance, immutable paths, secrets, Git internals and traversal remain denied. | `test_a10_project_maintenance_is_explicit_while_trusted_and_secret_paths_stay_denied`; fresh authority hidden workflow and secret-path tests. |
| A11 | Explicit `campaign.review_provider="gemini"` routes the review stage to a separately budgeted read-only frontier client. It receives actual candidate context and source tools. Source coverage, independent checks and verification gates still decide acceptance. Default legacy campaigns retain their local reviewer. | `test_a11_frontier_review_is_paid_identity_visible_and_cannot_invent_tools`; existing campaign source-coverage and trusted acceptance tests. Protocol errors are distinct from transport failures. |
| A12 | One SQLite event ledger reserves the immutable parent cap before supervisor, review and repair calls. Known usage settles; unknown outcomes remain reserved. Linked generations reuse the original ledger. Reports show aggregate known spend and pending exposure. | Concurrent all-role cap/pre-call tests in `test_autonomous_audit_controls.py`; fresh authority three-generation cap/reporting regression. |
| A13 | Distinct protocol, hypothesis, authority, context, provider, environment and verification failures. New repository intakes require measured failure, known evidence and a concrete changed hypothesis before retry; repeated same-candidate intent is denied. Actual candidate changes and new evidence pages count as progress. New repository default is 48 decisions; frozen old policies are not expanded. | `test_a13_retry_requires_measured_failure_and_a_changed_evidenced_hypothesis`; worker failure classification tests and fresh paging regressions. Hypothesis wording is model-provided; hard retry/decision/time/cash limits remain the final bound. |
| A14 | Server-owned execution service with explicit arm/start, restart recovery, kernel ownership lease, independent heartbeat, dependency preflight, queue yielding and visible UI runner status. Journal state remains authoritative; a runner record is only process liveness. | Arm/restart/dependency regressions; fresh service lease, idle queue, expired heartbeat, corrupt record and heartbeat completion race regressions; UI production build. |
| A15 | Added explicit live qualification infrastructure with frozen allocations/repetitions, all four scenario types across repository shapes, real scenario evidence, human/protocol counts, candidate receipts, exact supervisor/local request records and independent timeouts. Default invocation validates only. Passing deterministic tests cannot set a live autonomy result. | Fresh service qualification scenario, repetition, threshold, actual event metrics and paused timeout regressions; local provider request recording regression. Live repeated qualification remains unperformed. |

## Fresh independent review and hardening

Three reviewers started with no inherited conversation context and inspected core recovery, authority/accounting, and service/qualification behavior. Their findings produced additional regressions in:

- `tests/unit/test_audit_core_regressions.py`: interrupted milestone seed recovery, byte/mode and manifest tampering, page progress/stagnation, large retrievable intake context.
- `tests/unit/test_audit_authority_regressions.py`: optional questions before a first verified candidate, correct experiment seeds, narrowed maintenance permissions, hidden configuration, shared multigeneration cash cap, protocol classification and linked continuation.
- `tests/unit/test_audit_service_regressions.py`: queue fairness, exact current-version reporting, noncosmetic scenario qualification, complete distinct repetitions, observed intervention counts, frozen timeout, service ownership and heartbeat races.

All substantive findings reported by these reviews were addressed. The service reviewer reached its usage limit during a final follow-up; the primary agent completed the outstanding runner fixes and verification. No claim is made that independent review proves the absence of every possible defect.

## Operational behavior

Opening the console does not authorize inference. Create a reviewed intake using the existing CLI, then explicitly start its runner through the goal UI. Dependencies, credentials, approved sandbox capabilities and provider policies must pass preflight. Local coding and explicit frontier review remain separate roles under the same paid cap. Ordinary working directories and subprocesses are not described as strong isolation.

For qualification, use the module `loopforge.entrypoints.autonomy_qualification` with `--plan PATH --directory PATH`. It validates the frozen plan with zero calls by default. `--live` is an explicit execution opt-in and requires a fresh artifact directory. A qualifying plan contains all four scenarios, at least two repetitions and repository shapes, independent milestone criteria, a concrete wrong-hypothesis target, and aggregate allocations within its frozen cap. Its recorded evidence must demonstrate those behaviors; naming a case is insufficient.

## Verification

Validation completed with no paid or local model inference:

| Check | Result |
| --- | --- |
| `.venv/bin/pytest -q` | 3,175 passed, 67 skipped, 175.88 seconds. |
| Final affected authority/investigation regressions | 43 passed, including both pre-worker and existing-candidate continuation cases added during the full run. |
| Final core/service/control regressions | 32 passed. Together the final focused checks cover 75 cases. |
| `.venv/bin/ruff check src tests` | Passed. |
| `.venv/bin/ruff format --check src tests` | 259 files formatted. |
| `.venv/bin/pyright` | Zero errors or warnings. |
| `.venv/bin/lint-imports` | Both architecture contracts kept. |
| `npm run build --prefix ui` | Passed. |

The full suite's skips concern optional/live or unsupported host capabilities; they are not counted as verified execution. The latest continuation baseline fallback was also checked in the final focused run after the full suite had collected tests. Temporary detailed logs are under `/tmp/lf-audit-*`; these are local execution logs, not permanent benchmark certification.

No commit, push, application trial, or inference launch was performed. Existing unrelated working-tree changes were preserved.
