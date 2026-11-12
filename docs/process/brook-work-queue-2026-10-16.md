# Brook hardening work ledger — 2026-10-16

This is the finite operator-managed queue for the current Brook-only pass.
The generic 210-unit review schedule is finished. These entries are distinct
from its old exhausted tasks; no attempt or budget was reset. The local model
and n8n execute one repository and one worker at a time. HelicOps remains parked.

| Item | State | Evidence and next action |
| --- | --- | --- |
| README Water Rights Explorer source claim | Assessment rejected at one-attempt cap | `issue_677ab9b85462`, run `run_be1ac7e3d4fa`: route/client/RPC links were source-consistent, but the report omitted the required uncertainty about “live today” and “~145K”. Those deployed claims remain unverified. Do not count this as full documentation certification or recreate the issue to evade its cap. |
| `ResearchStore.list_water_rights` risk-score docstring | **Fixed and locally accepted** | `issue_ad91c87177e8` confirmed the docstring versus current v3.3 SQL/API/test contradiction. `coding_3365e68251ad` changed only that docstring; baseline-failing fixed check and fresh patch review passed. Accepted Brook commit `5dec493ca78e33db0cad84005c86ef5532d03b30`. |
| Upload cleanup on duplicate insert | Open defect | Original `coding_422e6add503b` rejected/exhausted. Preserve its cap and evidence; any separately authorized future work needs a distinct scoped contract and baseline-fails/candidate-passes check. |
| ICS hydration partial success | Open defect | Original `coding_a8f617c6bf25` rejected/exhausted. Preserve its cap; investigate the exact mixed-failure path before a new implementation effort. |
| Logout rejection leaves pending UI | Open defect | Original investigation and `coding_4dbfb2d1fbda` rejected/exhausted. Preserve their caps; require actual rejection/retry behavior and a fixed check. |

Native n8n parent/child executions `495`–`501` for these focused rounds and
the coding/review pass succeeded; full Hub downloads are retained under
`.loopforge/campaign/monitor/implementation/check-e1f09f6f-7a65-4ad6-a3fd-255a174a0d88`.
The first README report still failed its task-specific quality gate despite
transport success. The isolated A3B prompt probe and LoopForge unit suite do
not establish general autonomous discovery-to-repair quality.

The trusted coordinator described as A07 in
[the deeper audit](brook-end-to-end-audit-2026-10-13.md) remains to be built.
Until then, an operator must independently pair sources, define immutable
checks, close/reject investigations, inspect actual candidate patches, and
record local acceptance. A new model report cannot grant itself edit paths,
budgets or approval authority. Avoid another broad generic review launch.
