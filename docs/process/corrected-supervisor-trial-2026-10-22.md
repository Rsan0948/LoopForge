# Corrected supervisor continuation

Date: 2026-10-22

## Corrections

- Identical repair decisions are rejected by both the model wrapper and the tool
  executor. The next request receives the rejection; genuine revisions remain
  allowed. A network-free HTTP-boundary regression verifies rejection, feedback,
  revision, and successful editing.
- Specialist advice for a different candidate no longer injects obsolete edit
  instructions. Its identity and provenance remain in context, and the complete
  original advice remains in its ledger. Current-candidate advice remains available.
- Repository worker guidance now contains the measured baseline failure excerpt,
  checkpoint identity, and omission flag, alongside the supervisor's current plan.
  These are lower-trust observations and suggestions, not execution authority.
- Prepared goals also forward supervisor guidance, refreshed at execution time so
  constructing a worker before inference does not freeze an old plan.

The new trial disables local `repair_decisions`, `force_repair_attempt`, and
`revisable_repair_decisions`. The investigative frontier supervisor manages the
approach; the local worker can select authorized reads, checks, and edits without
the two-read forced-edit bottleneck. Existing frozen goals were not rewritten.

## Verification before launch

| Check | Result |
| --- | --- |
| Full pytest suite | 3,181 passed, 67 skipped; 182.26 seconds |
| Ruff lint | Passed |
| Ruff format | 260 files formatted |
| Pyright | Zero errors or warnings |
| Import architecture contracts | Both kept |
| Pinned container readiness | Passed |
| Local model / credential preflight | Passed |

The skips are not counted as verified behavior. Deterministic tests and preflight
do not establish successful real-model completion.

## Live trial configuration

Directory:
`.loopforge/tasks/reconciliation-forced-attempt/corrected-supervisor-2026-10-22/`

- Uses **RepositoryGoalService**, not PreparedGoalService.
- Persisted policy enables investigation and requires recovery evidence.
- Preserves the terminal predecessor's latest candidate, including its later edits.
- All previously required deliverable files were checked to exist before snapshot.
  They are now baseline files, not files that must be changed again for a repair.
- Original behavioral acceptance probes, fixed commands, container image, and
  allowed edit paths remain unchanged.
- The three already-authorized app test files are explicitly editable project files
  because they are tracked in the new snapshot. Independent acceptance is unchanged.
- Frontier review uses Gemini; local coding remains Qwen 3.8 27B.
- All paid roles share the original ancestral ledger. At preparation, the original
  $0.725678 envelope had $0.163996 spent and **$0.561682 remaining**, with no pending
  reservations. The new trial does not create additional spending authority.
- The trial imports a frozen, hashed copy of the verified runtime. Exact supervisor
  and local request payloads are recorded separately.
- No reconciliation application repair was made by the primary assistant. The live
  worker is responsible for improvements to the preserved application.

The previous run had already become terminal `blocked`; no live run was cancelled
or terminal state reopened as part of this launch.

## Live handoff correction

The first launch attempt resolved the virtualenv interpreter symlink and therefore
could not import `httpx`; it made no model calls. Launching through the actual
virtualenv path corrected that process setup error.

Gemini then prepared the environment, reproduced the current failures, retrieved
their output by byte offset, and inspected source. This confirmed investigative
supervision was active. All five baseline checks executed: document inspection and
launcher checks passed; project, API, and browser checks failed.

During inspection of the recorded evidence, the primary assistant found that the
baseline's first 2,000-byte page contained successful document inspection rather
than the failed checks. The run was paused before worker delegation. Worker
handoff was corrected to select bounded failed-check output from the complete,
integrity-checked baseline artifact. A regression specifically places the failure
beyond a long successful first page.

After **39 focused tests**, Ruff, and Pyright passed, execution resumed using
`runtime-v2/src`, `frozen-runtime-v2.json`, and `run-v2.py`. The first frozen runtime
and all events remain preserved. No goal policy, candidate, or paid allowance was
changed; no research was reset. This additional fix was validated after the full
suite quoted above, so the full-suite count is not presented as a rerun of v2.

## Observation

```sh
.venv/bin/python .loopforge/tasks/reconciliation-forced-attempt/corrected-supervisor-2026-10-22/observe.py
```

The trial also records `run.log`, `latest-state.json`, `runner.sqlite`,
`supervisor-prompts/`, `worker-prompts/`, `launch-manifest.json`, and
`frozen-runtime.json`. A terminal result produces `final-report.json`.

Launch alone is not completion. Observe the actual baseline investigation,
assignment guidance, useful worker actions, independent checks, and source review
before judging the trial successful.

## Observed terminal result

The trial stopped at goal event 79 with `Aggregate inference budget exhausted`,
after 25 supervisor decisions and before worker delegation. The parent ledger
contained $0.588604 of aggregate known spend and $0.137074 unspent. The next call's
maximum reservation exceeded that remainder; no extra call was made. This is a
failed autonomy trial, not a completed application repair.

The investigative supervisor worked, but spent too much of the small inherited
allowance paging through source before assigning work. Thus the worker evidence
handoff is regression-tested but was not reached by this live trial. Frontier
review was configured but likewise not reached.

After this result, supervisor guidance was strengthened to delegate a bounded
diagnosis/repair once failures and relevant source are known, rather than surveying
the entire app. Requests now expose remaining decision count and supervisor
allocation, explicitly warning that the shared all-role cap may be lower. No
authority or budget was expanded. Nineteen provider-adapter tests passed after
this additional change; it has not yet been live-qualified.

The user was asked to authorize up to $1 for a separately funded continuation.
No additional funding or inference is assumed while that request is pending.
