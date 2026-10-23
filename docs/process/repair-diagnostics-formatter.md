# Repair diagnostics and bounded import formatting

The stopped reconciliation candidate is preserved at revision
`da6976312e3385f76445b05e7c4737a3af3d636c`. The stopped recovery archive is
`.loopforge/experiments/repair-ablation/stopped-recovery.tar.gz`, SHA256
`eea17de0a56da718ae78b75fb606acf709a528394330bc39a2eaccd57ddeef88`.
The primary assistant has not repaired the application or transplanted experimental
repairs into it. A successor uses these latest model-authored bytes.

## Confined comparison

The six-sample comparison in `.loopforge/experiments/repair-ablation/report.md`
used two greedy repeats for each condition on one preserved import-sorting defect.
Noisy diagnostics without thinking fixed neither import error. Clean diagnostics
without thinking fixed one of two errors in both samples. Clean diagnostics with
thinking exhausted 4,096 output tokens twice without a native tool call. No
condition passed the independent whole-file repair oracle. These are fixture
results, not population estimates of model capability.

The user authorized a larger thinking budget. The follow-up at
`.loopforge/experiments/repair-thinking-8192/` replays the archived clean-thinking
request with only `options.num_predict` changed to 8,192. Its timeout is 1,200
seconds. It allows at most two requests, stopping after the first if the model
exhausts its budget without a native call. The runtime and inputs are frozen
privately, permitting independent harness hardening without contaminating the
comparison. The independent oracle, not the model's narrative, determines success.

## Harness changes

Fresh-state verification evidence now renders newlines as readable indented lines
while preserving untrusted authority. Feedback refers to the canonical verification
diagnostic rather than repeating the full failure text. The prompt is versioned
`1.5.0-state`; legacy profiles retain their previous tool exposure.

Fresh-state coding registers `format_python_imports`. Its arguments are an allowed
Python path and the current SHA256, never a command or formatter options. It uses
the pinned workload container, read-only source, isolated filesystem and network,
fixed Ruff `--isolated --select I001 --fix`, and temporary output. It compares
non-import AST plus imported names, aliases and lexical scopes, then delegates a
single replacement to the existing scoped, fingerprint-checked edit mechanism.
It denies protected paths, stale fingerprints, no-change results and arbitrary
options. No retries are declared. Ordinary fixed verification still applies;
structural preservation is not proof that arbitrary import reordering is harmless.

A real-container disposable qualification fixed both I001 errors, introduced no
new lint issues and changed only `app/server.py`; repeated formatting did not
write. This is tool qualification, not evidence that the model selected the tool
or completed the app. Its evidence is in
`.loopforge/tasks/reconciliation-formatter/formatter-qualification.json`.

Scripted integration campaigns now use a per-test model lease, matching the unit
suite. The first full run exposed eight conflicts with the live experiment's
global lease. The production exclusion lock remains unchanged; scripted tests
share a test-owned lock within each test and never claim the live model resource.

Validation: the full suite passed 3,050 tests with 67 platform/environment skips.
Four additional formatter failure cases were then added; the final focused coding
and formatter suite passed 83 tests. Ruff lint/format, Pyright and both import
contracts passed. The pinned readiness probe passed while project/API probes
retained their expected failures on the untouched candidate. The final formatter
qualification also passed after isolating both Python invocations with `-I`.
