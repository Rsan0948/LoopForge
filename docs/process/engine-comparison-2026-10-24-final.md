# Final engine head-to-head, 2026-10-24

The user asked for MLX as a production engine, a supervised MLX trial ($1.50 cap,
authorized 2026-10-24), and one last Ollama smoke under the same conditions as a
head-to-head. Everything below uses the reconciliation repair seed from the engine matrix.

## Supervised trials (Gemini supervisor and reviewer, local worker)

Both trials used the same seed tree, fixed checks, acceptance probes, objective (with the 5.7
wording) and $1.50 cap. Only the engine and its production usage budget differ (Ollama 32K,
MLX 48K).

| | Ollama f16 (`supervised-v3-2026-10-24`) | MLX (`supervised-mlx-2026-10-24`) |
|---|---|---|
| Runtime | `53da0d4`, frozen copy | `784be70`, via `launch` |
| Outcome | **delivered** | **delivered** |
| Supervisor decisions | 6 (prepare, reproduce, inspect, delegate, verify, deliver) | 9 (adds 3 `retrieve` decisions paging truncated baseline output) |
| Wall time, first supervisor prompt to report | 431 s | 424 s |
| Worker actions | 12 (10 reads) | 12 (10 reads) |
| Spend | $0.401 (supervisor $0.138, review $0.263) | $0.440 (supervisor $0.182, review $0.259) |
| Patch | Mobile CSS; wrong-job test 409→422; calculation test corrected | Same two test corrections; different mobile CSS with no `overflow: hidden` |

Both patches were checked by hand against CONTRACT.md: supported total = 70000 + 20000 +
10000 = 100000, so the variance on 120000 is 20000; a wrong job ID returns 422 (contract line 27).
The independent API probe still covers the same-job conflict 409.

The supervised runs are equivalent: the same time, and $0.04 apart (the 3 extra Gemini
paging decisions). With the supervisor's assignment, the worker needs only about 12
actions on either engine, so engine speed has little room to matter.

## Unsupervised smokes (local only, no paid calls)

All runs are one cold trial on the frozen full-a3b task, 60-turn cap, using each engine's
production configuration: the MLX adapter and service, the 10,240-token reasoning cap, and a
48K budget on MLX; 32K on Ollama f16. MLX runs restart the service (empty prompt cache).
Ollama runs unload the model and stop the MLX service, so both engines see the same free
memory.

| run | commit | engine | result | wall | calls | max prompt | compactions | notes |
|---|---|---|---|---|---|---|---|---|
| `final-smoke-mlx-20261024-0836` | `784be70` | MLX | **pass** | 451 s | 21 | 39,277 | 0 | max output 2,585 tokens |
| `final-smoke-ollama-20261024-0856` | `784be70` | Ollama | fail (no new evidence) | 221 s | 15 | 28,976 | 0 | **Harness defect**: a wrong path voided a multi-file read, then the model JSON-quoted single paths; fixed in `0153995` |
| `final-h2h-20261024-0904` (Ollama half) | `0153995` | Ollama | **pass** | 506 s | 32 | 32,323 | 1 | decode min 35.0, median 42.9 tok/s |
| `final-h2h-20261024-0904` (MLX half) | `0153995` | MLX | stalled (no progress) | 557 s | 24 | 38,167 | 0 | Model outcome: an edit broke `test_reconciliation_and_persistence`, followed by edit/revert cycling |

Neither engine had a speed failure. No call ran below 20 tok/s, and no call reached the
reasoning cap: the largest output was 4,906 tokens (Ollama) and 2,585 (MLX).

## Conclusion

- **Supervised (the way the loop will be used):** both engines delivered the full repair in
  about 7 minutes for about $0.40–0.44. That is a tie.
- **Unsupervised:** each engine is 1 for 2 on the final harness, and the losses differ in
  kind: Ollama's was the multi-read defect (now fixed), MLX's was the model. With one or
  two runs per engine this does not separate them, and unsupervised pass rates on this task
  have been about 60–70% for both since the roadmap work.
- **The engine choice is an operational preference, not a performance one.** MLX gives flat
  decode to about 48K, the larger 48K budget and the reasoning cap. Ollama has the longer
  production history and no lab interpreter dependency. The user chose MLX. To use it, set
  `"engine": "mlx"` in the intake campaign; `scripts/mlx_service.py status` checks the
  service.
- **What moves results is supervision and task-solving behavior.** Remaining unsupervised
  failures are the model breaking a passing test or cycling. The supervisor's precise
  assignment is what avoided both in the delivered runs.
