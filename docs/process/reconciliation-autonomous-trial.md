# Manufacturing reconciliation autonomous benchmark

The user authorized an audit, preparation, hardening round and autonomous launch on 2026-10-21. This is a synthetic local software benchmark, not a financial-accounting production system. The primary assistant prepares infrastructure, source placeholders, contracts, fixtures and independent checks; Qwen must implement and repair the application. A failed run cannot be relabeled delivered or rescued with primary-assistant app code.

## Workload

SQLite; distinct FastAPI HTTP/middleware, Pydantic contracts, PDF extraction, persistence and reconciliation-service modules; vanilla browser UI; Bash launcher; app-owned unit/PDF/API tests and pinned lint/test infrastructure. Three seeded provisional invoices reconcile against nine actual, structured, text PDFs. Purchase-order materials plus actual labor plus approved fixed-fee changes produce supported cents; variance is invoiced minus supported. This prevents scope changes from authorizing the same material/labor charge twice. Each accepted row retains document ID and original page. Missing documents require review. No tax, banking, OCR, cloud, runtime LLM or external assets.

The fixed HTTP/PDF/UI/lifecycle contract is in the isolated starter's CONTRACT.md. Newly created PDFs with changing values and multi-page rows are tested independently of the nine demo documents, preventing fixture-answer lookup. The API must strictly validate values, reject invalid or conflicting documents atomically, preserve identical reuploads idempotently, and persist state across restarts.

## Audit and preparation

- Preserve all existing unrelated working-tree work and production campaign services. No failed prior goal is reopened.
- Use the installed Qwen3.8 27B model and actual Gemini supervisor; prior cumulative journal estimate $0.642924. New immutable Gemini ceiling $1.25 remains within the user's $5 authorization.
- Add an operator-selected qwen-app-v1 profile. Coding/review controller context98,304; coding output4,096 and explicit incremental edits; separate reporter context196,608/output2,048, full serialized request ceiling176,000 bytes. Older profiles keep their limits. Mandatory evidence never disappears to make admission succeed.
- Provider timeout1,200s; coding/review run ceiling14,400s; task ceiling43,200s across three coding and three review attempts; goal ceiling57,600s,40 supervisor decisions; two million tokens/run and five million/task. Limits freeze before creation. These provide margins against observed local decode/prefill speeds, not a promise that every possible request finishes before timeout.
- Pin a Docker image containing Python/Node, FastAPI/Pydantic/uvicorn, pypdf/reportlab, SQLite, pytest, Ruff, httpx, Playwright and Chromium. No dependency installation or network is needed during checks. requirements.txt provides the local user setup.
- Retain read-only source/root, network none,2GiB memory,128 tasks and64MiB tmpfs. Writable SQLite databases/uploads/test temp files use /tmp. No new sandbox permission or kernel-isolation claim.
- Register a fixed check_documents action to extract trusted PDF text. The binary PDFs remain immutable fixtures outside app edit authority. Model/repository content cannot add shell commands or change the image/environment.
- Protected test rule audited: existing test files are immutable. The starter therefore has no tracked placeholder test cases; Qwen creates new app-owned tests, while external acceptance and test/lint config stay protected. Regression tests cover allowed new tests and denied existing/out-of-scope edits.

## Hardening before launch

- Actual adapter capability preflight: PDF parsing, SQLite write/reopen, pytest/Ruff, FastAPI process, real Chromium loopback request and clean shutdown passed under the runtime limits.
- Actual local model loaded196,608 context and produced a strict native-schema report.
- PDF output rendered and visually inspected; all nine document headers and extraction validated.
- Starter-negative checks: document inspection passes; project, API, browser and launcher acceptance all fail as expected. No false acceptance marker appeared; starter stayed unchanged.
- Actual source reader/review serializer admitted126,034 source-plus-diff bytes (48 required reads), rejected321,957 bytes (121 reads) with REPORT_PACKET_LIMIT, preserving evidence.
- Probe review corrected stale browser document-list waits, waited for actual wrong-job error responses, avoided reusing a just-closed port in the foreign-listener test, and disabled Ruff/pytest cache writes to read-only source.
- Final regression, type/lint/format/import checks are recorded under .loopforge/tasks/reconciliation/preflight/ before admission.

## Launch and evidence

Trial artifacts live under .loopforge/tasks/reconciliation/. Frozen intake, starter/fixture hashes, runtime/benchmark manifest, image identity, dependency lock, positive capability checks and negative probes are retained. run.py uses actual Gemini/Qwen inference; it contains no ScriptedModel or injected outcome. Only a terminal delivered state with runtime acceptance receipts and fresh source review counts as completion. Otherwise report the actual failure and retained checkpoint. Do not publish/install a candidate during this qualification.

Outcome: launched after all preflight gates passed. Frozen trial is running; no delivered result is claimed yet.

Final preparation checks: 3,021 passed,67 skipped,91.34% coverage; Ruff, formatting234files, strict Pyright0errors, both import contracts and whitespace checks passed.

Startup observation: one invalid Gemini assignment was rejected by strict validation and then corrected through normal bounded feedback. Qwen entered run run_c608175bb08f, read the frozen contract and successfully invoked check_documents on all nine PDFs. Recorded coding admission uses98,304 context/4,096 output; no primary-assistant candidate edits or runtime changes. Launch-status is in launch-status.json. The autonomous process runs until terminal state or its frozen limits; app completion is not yet claimed.
