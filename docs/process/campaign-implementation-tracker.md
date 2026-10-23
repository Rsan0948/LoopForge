# Campaign audit implementation

Authorized scope: implement the full-surface and model-performance audits dated
2026-10-12. Preserve existing workspace changes, immutable run limits, rejected
findings, exhausted tasks, sequential model execution, and parked HelicOps.

Campaign execution stayed parked until integration qualification passed. Brook
then resumed under the authorized scheduled check; HelicOps remains parked. Model
profile/backend experiments use isolated qualification tasks, never reset live
campaign attempts. This document records implementation evidence, not approval
to bypass the gates.

| Work package | Audit coverage | Status |
|---|---|---|
| Shared source pagination and exact report admission | F01 F06 | Implemented; source/read/report regression tests pass |
| Claim pairing, revision coverage, repository navigation | F03 F07; performance 1–2 | Implemented; paired claims, late anchors, least-covered selection and map tests pass |
| Structured retry memory and unified compaction | F08; performance 3 | Implemented; whole-record feedback and explicit compaction; real canary exposed and fixed oversized search/progress |
| Atomic coding completion and crash recovery | F09 | Implemented; rollback and legacy crash reconciliation tests pass |
| Shared model ownership and strict builder authority | F04 F05 | Implemented; shared lock and strict builder allow/deny tests pass |
| Launch/provisioning reconciliation and versioning | F10 F18 | Implemented; uncertain outcomes fail closed; idle templates installed with backups |
| Prompt batch continuation and compact incremental polling | F11 F12 | Implemented; stale-unit/idempotency tests pass; real 14-unit n8n continuation and terminal success qualified |
| Large output access and truthful/searchable UI | F02 F16 | Implemented and locally loaded; 21 MB execution download verified; authenticated browser checks passed |
| Role profiles, reasoning protocol, telemetry/context accounting | F13 F14; performance 5–6 | Implemented; baseline retained; full request accounting and saved-request calibration; candidate coding profile rejected; reporter v2 qualified |
| Outcome qualification, stage checks, measured optimization | F15; performance 4,7 | Implemented; six corrected review cases and real seeded Brook repair pass; 36/36 development and 24/24 fresh held-out review trials passed |
| Monitor delivery/heartbeat recovery and current handoff | F17 | Implemented; monitor tests pass; hold supersedes historical resume instructions |
| Backend/cache/drafting comparison and restore qualification | performance setup; operations | Backup/restore replay verified; backend migration deferred pending measured qualification; setup experiments pending |

No speedup or quality improvement is claimed until measured. Optional backend
changes must earn promotion on the same frozen tasks with equal authority.

## Final gate and operating state

All blocking implementation packages are installed and tested. Final suite:
2,788 passed, 50 platform skips; 66 Hub/monitor/installer tests; Ruff/format,
Pyright, JavaScript syntax and diff checks clean. See
[campaign-implementation-results-2026-10-12.md](campaign-implementation-results-2026-10-12.md)
for the completed source/quality/transport/restore evidence and limitations.

Final real n8n model canary: isolated coding and fresh review succeeded, fixed
checks passed again independently, and n8n ended successfully (Hub artifact775).
Restored API drill: eight exact candidate reconstructions and two campaign states
passed using the raw-patch replacement backup. Original held-out fixtures used
for debugging are development data; the fresh24-trial suite was not tuned against.

Scheduled check86039b24-4f03-4067-9c28-5ed8dc03ecbb recorded the audit-cancellation
correction and resumed Brook through normal APIs under existing caps. HelicOps
remains parked; rejected/exhausted repairs remain untouched. Optional alternate
backend, precision/cache and drafting experiments are deliberately unpromoted,
not blocking bug fixes or claimed speedups.
