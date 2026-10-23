# Campaign implementation and qualification

The full-surface and model-performance audit fixes are installed and qualified.
Brook resumed under the scheduled check authorization after the gates passed;
HelicOps remains parked. Qualification used isolated
copies and separate ledgers; existing acceptance, rejection and attempt limits
are preserved. This report distinguishes implementation from measured readiness.

## Changes

- Shared source pagination handles long lines, empty files and continuation.
  Admission uses the same mandatory reads and complete reporter HTTP serializer
  as execution, including schemas, instructions, escaping and retry feedback.
  Oversized evidence is denied before reserving a review attempt. Coding changes
  must also fit fresh-review capacity before executable checks.
- Documentation claims are paired with likely implementation, including claims
  late in a document. Coverage selects less-inspected intervals at the pinned
  revision. A bounded symbol/dependency map guides exact source reads. Broad
  content searches now paginate bounded hits; filenames belong in read_file.
- Feedback preserves whole records and explicit omissions. The coding context
  carries the frozen base, allowed paths, check names and completion contract.
  Progress diagnostics are previews with full bounded feedback available on
  demand. Compaction states what was omitted. Optional thinking replay preserves
  small validated responses and omits oversized reasoning whole with a notice.
- Single-file multi-edit validates every replacement and the expected source hash
  before one atomic write. This does not promise multi-file atomicity or strong
  host isolation. Check receipts require explicit deterministic opt-in and exact
  workspace, command, image, environment, baseline and effective timeout. They
  are one-use; fresh final verification cannot inherit coder receipts.
- Coding completion, campaign completion and slot release share a transaction.
  Recovery reconciles durable outcomes without resurrecting terminal states or
  charging a second attempt. Retry correction receipts name the actual failure
  and loaded implementation, preserving the original caps.
- Campaigns, the Hub builder and qualification/benchmark clients share a process
  lease. Builder permissions are strict and scoped, actions are journaled before
  mutation, and uncertain deliveries are reconciled or held for attention.
  No uncertain execution/credential creation is blindly repeated.
- n8n polls compact checkpoints, rejects stale unit identities, carries one
  launch identity through bounded batch continuation, and leaves authoritative
  results in LoopForge. Archival is incremental and large outputs remain
  downloadable. Source/template identities distinguish installed from loaded
  code. The installer backs up before changing idle transports and verifies both
  templates before committing their hashes.
- The Hub shows the accepted baseline and actual inference profile, supports
  server-side search and history pages, exposes full execution downloads and
  provider timings, and holds history while it is being read. Monitor delivery,
  heartbeat and acknowledgment are separate, identity-checked states.

## Measured evidence

Final offline suite: **2,788 passed, 50 platform skips**. Hub, monitor and
installer: **66 hermetic tests passed**. Ruff, formatting, Pyright (zero errors),
JavaScript syntax and diff checks passed. Loaded service identities match source.

The real Brook canary uses the existing accepted calendar image/check and tests.
The accepted source passes; a controlled UTF16 regression fails. A3B then repairs
only the permitted source file; the unchanged checks and fresh source/diff review
must pass against the same patch. This never reopens an exhausted campaign task.

| Experiment | Outcome | Time / coding tokens |
|---|---|---|
| Initial broad search | Failed: 35 KB tool observation exceeded the request bound | 44 s / 15,079 |
| Paginated search, large progress context | Failed: context envelope | 55 s / 30,138 |
| Bounded search, successful baseline canary | Checks and fresh review passed | 137 s / 57,496 |
| Frozen task packet and bounded diagnostics | Checks and fresh review passed | 44 s / 9,330 |
| Candidate sampling + thinking replay | Failed: latest reasoning alone exceeded the envelope | 223 s / 41,849 |

These are small experiments with evolving harness code, not a controlled general
speedup estimate. The candidate profile is **not promoted**. Its oversized
reasoning replay is fixed; it needs another qualification before adoption.

The real n8n transport test uses a separate harness API and scripted reports on
pinned Brook source. Fourteen units completed with one launch identity across
**two parent and fourteen child executions, all terminal success**. Clones save
success results for evidence and are left inactive. This proves transport and
reducer mechanics, not model judgment. An earlier test shut down its isolated API
before the final poll; that result was corrected to failed and retained.

Evidence lives under `.loopforge/campaign/monitor/qualification/`:
`brook-repair-bounded-packet-baseline`, `n8n-terminal-transport`, and versioned
review suites. Failed V5, V7 and V8 review suites are retained. They exposed false
contradictions on matching values and incorrect uncertainty about literal source.
The host rejects recognizable matching scalar claims rather than silently
rewriting findings or relaxing the grader. Concise literal-source instructions
and the reporting profile passed **36/36 development regression trials** and
**24/24 fresh held-out trials**. The original held-out fixtures were used while
debugging, so they are now development data; only the separately frozen fresh
suite is described as held out. No prompts were tuned against that fresh suite.
These small constant-contract fixtures are not proof of general bug discovery.

The Hub opens archived run 320 and provides its full **21,271,316-byte** download.
Authenticated browser checks covered campaign visibility, output search, the
large execution viewer and held history pagination. Screenshots are under
`output/playwright/`.

## Setup decisions and limits

Keep **Ollama / qwen3.6:35b-a3b / Q4_K_M / 32K** as the baseline. Residency inspection
showed the entire approximately 22.4 GB model in VRAM and 56% system memory free.
This snapshot does not measure peak memory or prove a cache configuration optimal.

Saved-request calibration measured 144 requests, with at most 0.278 observed
prompt tokens per serialized byte. The conservative byte bound stays unchanged:
these fixtures cannot certify unseen Unicode, code, templates or cache semantics.
An exact-renderer counter extension exists, but no aligned tokenizer is claimed.
The benchmark has explicit drafting on/off and 32K/64K experiments, records timing
separately from quality, and holds the shared lease. Requested options alone do
not prove the backend actually used speculative decoding.

The implemented repository map borrows Aider's retrieval approach, without
vendoring another orchestration stack. Serena remains optional if this retrieval
proves insufficient; adding a language server needs its own capability contract.
MLX is a later measured backend pilot, not an automatic replacement. Its upstream
cache/correctness/memory reports warrant version pinning and the same qualification
suite, rather than assuming a faster backend is compatible with this model.
See the [model-performance audit](model-performance-audit-2026-10-12.md) for primary
research and the proposed experiment order.

The restore drill verified consistent ledgers, reconstructed all eight candidate
checkouts with exact patches/added files, and checked both restored API projections.
It executes no repository workloads and does not restore production. A discovered
newline-stripping defect in patch backup was corrected with raw-byte capture and
a CRLF/apply regression. The earlier backup is retained with this limitation. The
verified replacement is in
`.loopforge/campaign/monitor/backups/audit-implementation-20261012-raw-patches` and
`~/ai-platform/backups/campaign-audit-upgrade-20261012-reconciled`.

## Completed restart gate

Do not restart live campaigns solely because offline tests pass. Require the
repeated outcome suite, a real repair with unchanged checks and fresh review,
terminal n8n transport success, accessible artifacts and loaded identity checks.
Existing rejected findings stay rejected; capped tasks stay capped. HelicOps
remains parked. No merge, push or product deployment is part of this change.


The final real-model n8n canary (`n8n-real-brook-repair-final`) completed actual
A3B coding, immutable checks, a separate fresh review, another independent check,
and terminal-success parent/child executions. Coding took 39.72 seconds and
11,816 cumulative tokens; fresh review took 6.40 seconds and 1,261 tokens. The
candidate changed only the UTF16 increment. It reached `patch_ready` in the
isolated ledger; it was not accepted into the live baseline. Hub artifact **775**
contains the receipt. The 14-unit scripted transport receipt is artifact **762**.

The earlier real n8n canary exposed production patch review continuing controller
calls after mandatory coverage; it exhausted its review allowance despite a good
patch. PatchReviewVerifier now selects mechanical finalization at coverage, with
normal source validation, blockers and independent checks unchanged. Failed
canary evidence is retained; no failed task counter was reset.

Before restart, exact row comparisons confirmed both campaigns, all 713 campaign
events, eight coding contracts, 107 coding events, seven issues and 40 issue events
unchanged from the original backup. Scheduled check
`86039b24-4f03-4067-9c28-5ed8dc03ecbb` then recorded a failure-linked correction for
the audit cancellation, resumed Brook via domain/operator APIs and launched once
through the authenticated Hub. Original caps and accepted baseline remain intact.
Brook advanced from cursor 106 through 109 on the first inspection. Completed
review units retain unresolved tests/behavior questions; they are assessments,
not accepted repairs. HelicOps remains parked. No push, merge or product deploy.

Final check-in inspection: Brook reached cursor120 with one occupied slot owned
by Brook; HelicOps remained parked. Check86039b24-4f03-4067-9c28-5ed8dc03ecbb
was acknowledged. Implementation report is also saved in Hub artifact789.
