# Full campaign surface audit — 2026-10-12

Audit/report only. No implementation, configuration, workflow, model or campaign-state changes were made for this audit. Earlier remediation edits remain in the workspace; this report does not mean they have been loaded by the running service. Neither campaign was resumed. The existing scheduled-check begin/ack bookkeeping is separate from campaign execution.

The central finding is that local correctness checks are stronger than the contracts **between** components. Evidence selection, report capacity, run archival, scheduling and model ownership do not yet form one consistent end-to-end contract. A faster model alone will not fix those gaps.

## Scope and evidence

Inspected the campaign API, planner, evidence reader, reporting pipeline, coding/review coordinator, domain transitions, SQLite projections, relevant runtime verification paths, Ollama adapters, container-check composition, both n8n templates, installed workflow status/settings, automation-hub backend and UI source, monitor and operational documentation, tests and qualification script. This is a broad audit of the campaign system, not an exhaustive audit of every LoopForge package or a new product-code audit of Brook/HelicOps. UI findings below are source-reviewed, not browser-tested. Concurrency/crash scenarios were traced without inducing a live crash or race.

Methods: source inspection; read-only SQLite queries; local status/GET requests; small in-memory probes of existing functions. No live inference, workflow execution, repair attempt or full test-suite rerun. The previous 2,763-passed/50-skipped result belongs to the preceding remediation and is not evidence that the findings below are fixed.

Live observations during this audit:

| Surface | Observation |
|---|---|
| Brook | Parked at cursor106; no worker, slot or runnable work |
| HelicOps | Parked at cursor6; no worker or slot |
| n8n | Running0, waiting0; no additional inventory pages |
| Parent/child | Installed and active; 21,600/18,000-second timeouts; full execution-progress/success/error retention |
| Campaign history | 637 Brook events; 106 completed generic review reports |
| Coverage history | 339 excerpt occurrences; 210 distinct exact intervals across121paths; these span multiple historical harness versions |
| Largest repeats | Three governing-document/header intervals each appeared16times |
| Storage | Hub DB832,978,944bytes; run DB31,031,296bytes; monitor heartbeat log6,888,989bytes |
| Read payloads | Campaign listing37,755bytes; latest20run activity315,331bytes; Brook ledger712,842bytes |
| Hub overview | One local sample:129,368bytes and2.165seconds; not a latency benchmark |
| Broken live page | GET `/api/runs/320` returned400: `Output is too large (8 MiB limit)` |

Prior fixes for comment masking, alias selection, deletion anchors, structured coding feedback and prepared-patch checking are present on disk. They should not be counted as newly unfixed findings. The limitations below include remaining gaps in those surrounding contracts.

## Findings and recommendations

P1 means address before relying on the affected campaign capability again. P2 means material reliability, efficiency or usability work. P3 means maintenance/operational improvements. “Reproduced” identifies an actual observed result; “source-confirmed” identifies a concrete code path whose live triggering conditions were not forced.

### F01 — P1: Review admission can permit a patch that reporting cannot finish

**Reproduced in memory.** Twelve mandatory source pages fit a64-read allowance, but constructing their report packet raises `REPORT_PACKET_LIMIT`. Coding admission checks page count; final reporting separately caps packet+schema+objective at26,000bytes. Mandatory evidence cannot be omitted to recover. The model can spend its attempt satisfying a contract that has no successful reporting path.

Evidence: [coding capacity check](../../src/loopforge/entrypoints/campaign_coding.py:367), [report packet](../../src/loopforge/entrypoints/campaign_review.py:636), [report envelope](../../src/loopforge/adapters/ollama_reporting.py:11). Probe:12files ×30short lines; `required_reads=12`, allowance64; report construction failed before inference.

**Recommendation:** Preflight the exact mandatory report payload, including source numbering, JSON escaping, schema, objective and reserves. Decompose before reserving an attempt when it cannot fit. A later hierarchical review design must preserve patch identity and coverage across its bounded subreviews. **Acceptance:** an admitted worst-case packet finalizes; oversized and Unicode-heavy packets are rejected/decomposed before work starts. Do not silently increase active budgets or drop mandatory evidence.

### F02 — P1: Large archived executions cannot be opened in the hub

**Reproduced live.** Execution320 has21,271,316characters of archived detail. `execution()` loads it, then tries to save a formatted copy through the8,000,000-byte artifact limit; the exception becomes an HTTP400. The archive exists, but its ordinary detail page fails. Other stored executions are larger, including175 at29,476,821characters.

Evidence: [execution retrieval/archive](</Users/rubensanchez/ai-platform/infra/automation_hub/service.py:249>), [artifact limit](</Users/rubensanchez/ai-platform/infra/automation_hub/service.py:125>).

**Recommendation:** Separate retrieval from artifact creation. Return a bounded execution summary and paginated node outputs; retain/download the full payload as a referenced large object. Archiving failure must not prevent reading an existing run. The8MB label also uses decimal bytes despite saying MiB. **Acceptance:** open and download actual execution320 and a larger fixture without truncating evidence or returning an error; small outputs retain current behavior.

### F03 — P1: Generic documentation work often has no implementation evidence

**Reproduced in memory; quality/design gap.** A document claiming200 and an implementation returning500 yielded a docs workload containing only the document. The limitation is now honestly disclosed, but `related_source()` derives relationships from imports, which ordinary Markdown lacks. Generic workloads also auto-finalize when those selected intervals are covered. Thus successful docs rounds can remain incapable of testing their claims.

Evidence: [pairing and planner](../../src/loopforge/entrypoints/campaign_quality.py:93), [automatic finalization](../../src/loopforge/entrypoints/campaign_review.py:728), [workload setup](../../src/loopforge/entrypoints/campaign.py:728).

**Recommendation:** Build claim-to-implementation contracts: select the doc claim, referenced symbol/config/endpoint, current implementation and relevant test. Treat unpaired claims as a discovery backlog, not completed drift checks. **Acceptance:** feed matching, contradictory, superseded and deliberately unpaired docs through the actual planner→controller→reporter path; only paired contradictory evidence qualifies as suspected drift.

### F04 — P1: “One agent at a time” is not shared across the hub and harness

**Source-confirmed race/bypass; not exercised live.** Hub builder admission checks campaign status before taking its own lock. Campaign launch checks builder rows under that lock, but the harness `/tick` endpoint and an already-running n8n parent do not share it. A builder admitted between campaign units can overlap the next model unit. The documentation already limits the harness slot to its own workers, but the hub promises broader exclusion.

Evidence: [builder admission](</Users/rubensanchez/ai-platform/infra/automation_hub/service.py:449>), [launch admission](</Users/rubensanchez/ai-platform/infra/automation_hub/service.py:380>), [harness claim](../../src/loopforge/entrypoints/campaign.py:359).

**Recommendation:** One durable owner/lease contract for all managed local model work, acquired at actual execution admission. A status poll is not a lock. Unmanaged direct Ollama clients must remain explicitly outside the guarantee. **Acceptance:** race builder admission against direct tick and n8n continuation; exactly one enters inference, including after process interruption. n8n's production concurrency setting alone is insufficient: its documented scope excludes subworkflows and manual executions.

### F05 — P1: Builder operation rules remain prompt-only in important places

**Source-confirmed authority/reliability gap; no unsafe action attempted.** The builder prompt says activation requires a user request, but `execute_tool()` accepts model-selected activation/run/update operations directly. `bool(args.get('active'))` also turns a nonempty string such as`"false"` into true. Workflow creation lacks a durable action identity for ambiguous responses. General workflow access includes campaign workflows.

Evidence: [builder prompt](</Users/rubensanchez/ai-platform/infra/n8n_dashboard.py:358>), [tool executor](</Users/rubensanchez/ai-platform/infra/n8n_dashboard.py:475>), [model loop](</Users/rubensanchez/ai-platform/infra/n8n_dashboard.py:588>).

**Recommendation:** Port LoopForge's strict arguments, code-owned metadata, scoped authority and idempotent action journal to the builder. Make campaign infrastructure protected from ordinary builder edits. Explicit user authorization should persist as a capability grant, avoiding repetitive permission prompts. **Acceptance:** string booleans denied; ungranted activation and protected-workflow mutation denied; granted operations succeed; replay after lost acknowledgement does not create duplicates.

### F06 — P2: Source-reading contracts strand long lines and empty files

**Reproduced in memory.** Coding a4,000-character line returns only its first3,000characters and instructs `Next start_line: 1`; the tail is unreachable with the offered selector. Separately, adding an empty source file produces obligation1–1 and `mandatory source interval cannot be read: empty.py:1`. This can block valid empty package markers. The review reader fails explicitly on oversized lines, but coding presents a continuation that cannot advance.

Evidence: [coding reader](../../src/loopforge/entrypoints/campaign_coding.py:270), [obligations/page counter](../../src/loopforge/entrypoints/campaign_evidence.py:90), [review reader](../../src/loopforge/entrypoints/campaign_review.py:540).

**Recommendation:** Use one evidence-selector contract with line/column or byte continuation, explicit empty-file evidence, and consistent provenance. **Acceptance:** every character of a long line is reachable without repeating the same selector; empty added/deleted files are reviewable through file metadata and diff without inventing source lines.

### F07 — P2: Traversal is weakly related to meaningful coverage

**Reproduced in memory; improvement opportunity.** The deterministic formula selects21primary files from a100-file focus in three rounds. Related files can broaden this, but seven angle labels do not create exhaustive or independent analysis. The history has substantial repetition, though much predates recent planner corrections. Candidate extensions excludeJS/JSX/YAML/JSON/shell, even where these carry deployment or contract bugs.

Evidence: [selection formula and extensions](../../src/loopforge/entrypoints/campaign_quality.py:145), historical counts above.

**Recommendation:** Keep a revision-bound coverage/claim ledger: unique files and symbols, exercised evidence types, unresolved boundaries, repeats and novelty. Select next units by risk and uncovered claims within approved scope. Add explicit handling or disclosed exclusion for configuration/scripts. **Acceptance:** seeded defects late in a large directory can be selected; repeated packets do not increase unique coverage; each lane explains what distinct question it tested. Preserve finite budgets and sequential execution.

### F08 — P2: Retry memory is structured but still loses useful failure information

**Source-confirmed.** Coding retries retrieve only the last two failed/retry/request-changes records. `coding_feedback()` excludes the `error` field used by exception paths. Issue handoff still takes the last8,000characters of serialized JSON; reporter prior evidence takes another tail slice. Those slices can lose record boundaries and the reason a prior strategy failed.

Evidence: [feedback field selection](../../src/loopforge/entrypoints/campaign_execution.py:41), [two-record retrieval](../../src/loopforge/entrypoints/campaign_execution.py:283), [issue handoff](../../src/loopforge/entrypoints/campaign.py:501), [reporter prior context](../../src/loopforge/entrypoints/campaign_review.py:688).

**Recommendation:** A bounded structured task memory containing current blocker, check identity, last tested patch, last failing assertion, attempted strategies and explicit omissions. Include classified infrastructure errors. Keep it untrusted evidence and link full records. **Acceptance:** three distinct failed strategies survive compaction as whole records; current correction is prominent; arbitrary repository instructions do not become policy.

### F09 — P2: Coding completion and campaign completion are separate transactions

**Source-confirmed crash window; no crash injected.** The coding result event commits, then `finish()` separately updates campaign state/releases the slot. A crash between these writes can leave a valid reviewed/patched task under a campaign still marked running. Recovery fails the campaign even though the task result may already be durable. Issue completion already uses one transaction.

Evidence: [coding completion](../../src/loopforge/entrypoints/campaign_execution.py:423), [campaign completion](../../src/loopforge/entrypoints/campaign.py:828), [atomic issue completion](../../src/loopforge/entrypoints/campaign.py:568).

**Recommendation:** One transaction for coding outcome, campaign outcome and slot release; explicitly reconcile already-durable outcomes after a crash. **Acceptance:** fault injection before/after each write neither loses a verified result nor causes duplicate work or terminal-state resurrection.

### F10 — P2: An ambiguous launch can remain frozen indefinitely

**Source-confirmed.** A launch marked`requested`or`unknown` returns an “existing” response on every later launch before inspecting fresh execution evidence. There is no matching reconciliation path in this service. Not retrying blindly is correct; having no durable way to settle the ambiguity is incomplete. `ensure_driver()` also persists workflow identity/path/secret in separate steps and treats an existing driver as up-to-date without comparing its template version.

Evidence: [launch guard](</Users/rubensanchez/ai-platform/infra/automation_hub/service.py:380>), [driver provisioning](</Users/rubensanchez/ai-platform/infra/automation_hub/service.py:348>).

**Recommendation:** A durable launch/provisioning intent with correlation ID, observed execution receipt and bounded reconciliation. Version installed parent/child workflows against approved templates. **Acceptance:** lost webhook acknowledgement converges to the existing execution or an evidence-backed retry decision; interruption between provisioning writes is recoverable without orphan duplicates. Never clear uncertain state merely because time elapsed.

### F11 — P2: Batch continuation adds avoidable idle time

**Source-confirmed scheduling design.** n8n stops after12units or30minutes. The monitor reconnects ready campaigns on its900-second schedule. A finished batch may therefore wait almost15minutes for routine continuation, even when no decision is needed. This delay is unrelated to Ollama generation speed.

Evidence: [parent batch guard](../../integrations/n8n/campaign-driver.json), [monitor reconnect](</Users/rubensanchez/ai-platform/infra/campaign_monitor.py:114>), operating timer configuration documented in campaign-monitor.md.

**Recommendation:** Let n8n own durable bounded-batch continuation; use the assistant monitor for quality decisions and recovery. Keep a checkpoint between batches and acquire the same single-worker claim. **Acceptance:** a ready batch continues promptly without a15-minute assistant cycle; parked/attention states do not dispatch; duplicate continuation notifications are idempotent.

### F12 — P2: Repeated full polling amplifies storage and UI cost

**Measured live plus source-confirmed.** Child polling stores full campaign status every2seconds with execution progress and all success data retained. The hub then archives detail and often stores a second formatted artifact. The collector reloads full campaign ledgers every20seconds; the UI reloads overview and campaign every8seconds. Present endpoints are fast, but large bodies and repeated full scans scale with history. The hub DB is already about833MB.

Evidence: [child polling/retention](../../integrations/n8n/bounded-unit.json), [collector](</Users/rubensanchez/ai-platform/infra/automation_hub/service.py:280>), [UI refresh](</Users/rubensanchez/ai-platform/infra/automation_hub/web/hub.js:69>), [activity replay](../../src/loopforge/entrypoints/campaign_visibility.py:88).

**Recommendation:** Compact task status for polls; persist task/run references rather than repeated full snapshots; incremental event cursors and projections; content-addressed large artifacts; retention tiers for transport traces versus authoritative events. Add measured query indexes after profiling. **Acceptance:** compare bytes, disk growth, refresh latency and recovery fidelity on the same long-run fixture. Retention must preserve accepted/rejected evidence and provenance.

### F13 — P2: Generation speed cannot be diagnosed from current telemetry

**Source-confirmed observability gap.** Provider tracking records request start/end and kind; response schemas preserve counts but discard prompt-evaluation, generation and model-load durations. Thus elapsed run time blends model loading, prompt ingestion, decoding, tools, checks, retries and scheduler idle time. The newer usage projection still labels `MODEL_CONTEXT_LIMIT` without debit as unknown usage, although the inspected Ollama paths raise that code before `_post()`.

Evidence: [request tracker](../../src/loopforge/adapters/ollama_usage.py:12), [controller response/request](../../src/loopforge/adapters/ollama_model.py:223), [report response](../../src/loopforge/adapters/ollama_reporting.py:19), [usage classification](../../src/loopforge/entrypoints/campaign_visibility.py:81).

**Recommendation:** Record model digest and role, prompt tokens/time, generated tokens/time, load time, known/unknown usage provenance, check time and idle time. Distinguish a local pre-request stop from an unobserved provider response. **Acceptance:** dashboards calculate decoding tokens/sec from generated tokens/eval duration and show throughput per accepted outcome separately. Do not claim hardware or model slowness from wall time alone.

### F14 — P2: Context and reasoning settings need calibrated qualification

**Source-confirmed optimization candidate; speedup unmeasured.** Coding reserves8,192output tokens in a32,768context, then compares serialized UTF-8 **bytes** with the remaining23,552token-sized envelope. This is deliberately conservative, but can compact far earlier than actual tokenizer occupancy requires. The context builder separately allows24,000estimated tokens; the builder uses49,152context; review/coding use different thinking settings. These settings are spread across adapters/composition roots.

Evidence: [coding settings](../../src/loopforge/entrypoints/campaign_coding.py:523), [provider envelope](../../src/loopforge/adapters/ollama_model.py:252), [builder settings](</Users/rubensanchez/ai-platform/infra/n8n_dashboard.py:595>).

**Recommendation:** Version role-specific profiles, measure actual token occupancy and model-load/prefill costs, then qualify conservative tokenizer-aware admission and smaller reasoning budgets for straightforward edits. Evaluate the already-chosen A3B model first. **Acceptance:** benchmark the same locked tasks with equal authority/checks; measure accepted repair rate, repeated reads, useful actions, latency and memory. No blanket context increase or relaxed limits is justified by this audit.

### F15 — P1 for qualification: The benchmark can reward the wrong finding

**Reproduced in memory.** For an expected-positive case, `grade()` accepts any suspected finding whose citations cover the expected file paths. It does not check the claimed contradiction. A fabricated unrelated claim with the correct two paths returned true. The fixture suite primarily tests simple documentation constants and feeds the intended files directly, so it also bypasses the generic planner failure inF03.

Evidence: [grader](../../scripts/qualify_campaign.py:144), [qualification setup](../../scripts/qualify_campaign.py:156).

**Recommendation:** Grade the expected claim/behavior, not just disposition/path coverage. Add realistic frozen fixtures for async recovery, TS imports, retained prepared patches, poisoned context, long-line/empty-file evidence, report capacity, crash recovery and end-to-end planner selection. Use meaningful broken-baseline/negative controls for repairs and separate held-out cases from prompt tuning. **Acceptance:** the demonstrated wrong-claim report fails; a correct finding and a clean negative case pass; results report discovery, repair and operations quality separately. Existing broad unit-test counts remain useful but are not model-quality qualification.

### F16 — P2: Hub status fields can mislead operators

**Source-confirmed.** Campaign creation displays “Qwen3.8 ·27B” while submitting no model field, so it actually uses the backend default, currentlyQwen3.6A3B. Campaign configuration displays the initial commit even after accepted local patches change the review baseline. Activity correctly returns the newest100timeline entries plus`next_event_offset`, but the page only follows run pagination and then shows the last70entries across the loaded runs. It offers no event-cursor control for older events within an individual run. An earlier audit suspicion that the API returned the oldest100events was checked and dismissed. The runs page filters only its loaded100rows despite backend search support and does not auto-refresh with the other live pages.

Evidence: [wizard/runs/campaign UI](</Users/rubensanchez/ai-platform/infra/automation_hub/web/hub.js:31>), [baseline calculation](../../src/loopforge/entrypoints/campaign_execution.py:153), [event pagination](../../src/loopforge/entrypoints/campaign_visibility.py:181).

**Recommendation:** Show effective model/profile and accepted baseline from server projections; render the current phase, blocker and next permitted action; expose the existing older-event cursor while retaining newest-first retrieval; search the full archive server-side. **Acceptance:** a>100-event run exposes both its terminal reason and earlier diagnostics, old-page search finds matches, and displayed model/SHA match the actual run.

### F17 — P2: Monitor freshness and handoff documents are inconsistent

**Source-confirmed; current document drift observed.** Supervision overdue logic uses queue age, not last heartbeat. A legitimately long active audit can therefore look overdue. The current monitor document still describes the third logout attempt as running and directs continuation, while live state is parked and that task is rejected/exhausted. Its recorded suite count is older too. The user's newer stop condition is controlling; this stale document is not permission to resume.

**Additional live evidence from the subsequent check:** snapshot `2026-10-12T21:28:59.984214+00:00` records check `fc5d8230-e107-410d-9f16-ab4d371c19ba` queued since16:28:54UTC, age18,005seconds, overdue=true and thread_status=`systemError`. Reconciliation only attempts to start an existing queued submission when the thread is idle; it does not recover this error state. This demonstrates a roughly five-hour supervision delivery gap despite the timer continuing to produce snapshots. The cause of the thread's system error was not established. After the user said Continue, the check was explicitly begun and live state was inspected; both campaigns remained parked, with no slot/worker or n8n running/waiting executions.

Evidence: [supervision calculation](</Users/rubensanchez/ai-platform/infra/campaign_monitor.py:75>), [heartbeat storage](</Users/rubensanchez/ai-platform/infra/campaign_monitor.py:174>), [operational handoff](</Users/rubensanchez/ai-platform/infra/campaign-monitor.md:20>), [newer remediation record](campaign-audit-remediation.md).

**Recommendation:** Separate queued-delivery age, active-heartbeat age and completed-check freshness; provide an explicit recovery/escalation path for thread errors, without duplicating ambiguously delivered work. Maintain a short current handoff with timestamp and pointers to immutable history. A durable campaign hold reason should be visible in status and constrain dispatch. **Acceptance:** a fresh heartbeat does not look abandoned; missing delivery and thread errors visibly escalate; a stale handoff cannot silently override a newer hold. A timer heartbeat must never be presented as completed assistant supervision.

### F18 — P2: Retry eligibility uses an incomplete and semantically weak fingerprint

**Source-confirmed.** Generic review retry compares hashes of a selected source-file list. Runtime/evidence changes outside that list are not represented; changing the configured model also leaves the hash unchanged. Conversely, an unrelated edit inside a hashed file can satisfy “corrective change required.” There is no health/status release manifest proving what code the live process loaded.

Evidence: [retry gate](../../src/loopforge/entrypoints/campaign.py:613), [fingerprint inputs](../../src/loopforge/entrypoints/campaign.py:691), [health response](../../src/loopforge/entrypoints/campaign.py:1011).

**Recommendation:** Record an execution manifest covering code/config/model digest/tool schemas/workflow version, and a distinct correction receipt linked to the actual failure. Verify compatibility before retry while preserving original caps. **Acceptance:** relevant correction/model change is recognized, cosmetic edits alone do not qualify as recovery evidence, and status distinguishes code on disk from code loaded in the process.

## Improvements to pursue after the blocking contracts

1. **Make progress a durable claim/repair graph.** Connect source observation → suspected issue → reproduction → bounded patch → fixed checks → independent review → local acceptance. Preserve rejected/exhausted defects as unresolved product work. A terminal task must never masquerade as a fixed defect. The existing events and assessment records provide a foundation; add explicit links and a user-facing rollup rather than another independent history.
2. **Package project check environments before attempts.** Record image identity, dependency readiness, exact immutable assertions and accepted baseline before reserving the coding attempt. Offer approved task templates in the hub so users do not need to invent Docker digests and argument arrays for routine repairs.
3. **Eliminate duplicate checks carefully.** An explicit check action is followed by the verifier running required commands again. Measure this cost. Reuse a result only when it is bound to the same immutable patch, command, image and environment; preserve fresh independent review and final acceptance checks. Do not equate a cached model claim with a check result.
4. **Use symbol-aware evidence retrieval.** Build/import a pinned dependency index once per revision, with proper TS parsing and explicit unresolved aliases. The current static regex/AST heuristics are useful fallbacks, not a full cross-language semantic index. Avoid lexicographically selecting a peer solely because it sorts first.
5. **Improve actionable UI summaries.** Show “what is happening / why it stopped / what evidence exists / what can happen next.” Distinguish queued, executing, waiting on a provider, awaiting a decision and parked. Avoid making large raw ledgers the primary troubleshooting interface.
6. **Treat release and restore qualification as part of operation.** Version parent/child workflow exports, harness configuration, model identity and check images together. Exercise restoring the two SQLite stores, candidate repositories and artifacts without reactivating terminal work. Backups and restore behavior were not independently qualified in this audit; this is a recommended check, not a claim that backups are absent.

## Recommended order and restart criteria

First repair F01/F02/F03/F04/F05/F15: finishability, evidence quality, model ownership, builder authority and trustworthy qualification. Then address evidence-reader failures, crash/launch recovery and UI truthfulness. After those contracts are tested, optimize polling, scheduling and model profiles using measured accepted outcomes per hour.

Before a future restart, require: every intended unit fits both reading and reporting; actual large archived runs open; managed inference shares a single owner; realistic positive/negative fixtures pass through the real planner and coding/review chain; the current hold/release identity is unambiguous; and a bounded qualification run is planned with retained failure evidence. This audit did **not** execute that run or grant any budget reset.

What is already worth preserving: append-only event/reducer state, transactional campaign claims, non-expiring slot ownership, strict model-boundary validation, lower-trust source evidence, pinned source/check-image identities, network-disabled checks, exact patch identity before acceptance, finite attempt limits, and staged local acceptance. The correct direction is to extend these mechanics across the remaining boundaries, not discard them or add more model agents.

## Primary-source cross-checks

- [n8n concurrency control](https://docs.n8n.io/hosting/scaling/concurrency-control/): production concurrency does not cover manual/subworkflow execution, supporting a shared application-level model owner rather than relying on a single n8n setting.
- [Ollama chat API](https://docs.ollama.com/api/chat): exposes load, prompt-evaluation and generation durations; these support separating prefill, decoding and loading instead of inferring speed from whole-run elapsed time.
- [Ollama context length](https://docs.ollama.com/context-length): context affects memory and model offloading. Profile changes require measured qualification on this host rather than an automatic context increase.
- [Anthropic: effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents): supports explicit durable progress artifacts and verification across work sessions. Applied here as a design reference, not proof that its model-specific performance transfers to local Qwen.

All recommendations above are proposals. No finding in this report was fixed during this audit.
