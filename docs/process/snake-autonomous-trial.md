# Local Snake delivery and autonomous trial

Date: 2026-10-21

## Delivered game

The complete application lives at `apps/snake`. From a fresh laptop terminal:

```bash
lets_play_snake
```

Or from the home directory:

```bash
bash lets_play_snake
```

Installed wrappers:

- `/Users/rubensanchez/.local/bin/lets_play_snake`
- `/Users/rubensanchez/lets_play_snake`

They invoke the published app launcher without a venv, Node/npm, downloads or shell
profile changes. It starts `http://127.0.0.1:8765`, opens the browser and runs in the
foreground. Ctrl+C stops it. A repeat invocation reuses the same app; a different
port occupant is preserved and reported. High score and the latest five completed
games stay in this browser at this origin. There is no hosting, account, game
telemetry or remote game storage.

Gameplay supports arrows/WASD, touch/mouse direction buttons, Space/P pause,
Enter restart, F fullscreen, Escape exit, background pause, food/growth, wall/body
collisions and a full-board win. The interface fits a laptop viewport and mobile.

## What the autonomous trial actually did

**This was not a successful unattended LoopForge build.** The final game was
completed by the primary coding assistant after the trial exposed framework and
local-worker limits. No failed goal was reset, reactivated or relabeled delivered.

Histories and immutable intakes remain under `.loopforge/tasks/snake/`:

| Directory | Outcome | Journaled Gemini charge |
| --- | --- | ---: |
| `goal` | Blocked after strict supervisor decision failures, before coding | $0.069318 |
| `goal-v2` | Blocked after local worker context admission failures | $0.112282 |
| `goal-v3` | Cancelled after unsupported single-process Chromium preparation | $0.047694 |
| `goal-v4` | Blocked after bounded local-worker attempts and context failures | $0.108246 |
| `assisted-goal` | Prepared verification handoff blocked by immutable review admission | $0 |

Additional Gemini charge for this task: **$0.337540** by the journal's conservative
accounting. Together with the previous $0.114208 estimate: **$0.451748**, within the
user's existing $5 ceiling. These are usage/accounting estimates, not an account
bill. The final coding, browser QA and advisory model review made no paid Gemini
calls. Trial ceilings and terminal histories were retained.

The actual trial established several limits and repairs:

1. New-feature admission was absent. Goal composition now explicitly permits a
   frozen `feature` task and the Snake project label; legacy campaigns retain
   their stage-one hardening-only rule. Allowed and denied cases are tested.
2. Nested supervisor schemas omitted domain inspection/assignment size bounds.
   Their advertised schemas now include those bounds. Safe, field-only failure
   feedback reaches the next bounded decision; strict validation remains intact.
3. Worker feedback included entire trusted browser probe programs and overflowed
   the local model's context. Feedback now includes bounded criterion metadata and
   source hashes; actual immutable probe execution remains unchanged.
4. Concurrent browser contexts and threaded test serving exceeded the existing
   128-task container ceiling. The separately recorded final browser harness uses
   sequential contexts and non-threaded serving, with the same read-only root and
   workspace, no network, 2 GiB memory, 128-task and bounded tmpfs contract.
5. The local worker still exceeded its conservative context envelope on larger
   edits and proposed multiple actions where exactly one was allowed. Runtime
   admission and one-action guards stopped those attempts. They were not weakened.
6. The broader prepared app exceeded existing runtime reporting admission. The
   guard preserved the candidate and stopped. The final app therefore has direct
   frozen browser/launcher verification and an explicitly advisory second-model
   review; it does not claim a successful runtime approval receipt.

This is useful Phase 5 evidence, not completion of the broad qualification or
production rollout gates. Larger-task context/decomposition and review admission
still need qualification before unattended use.

## Final application verification

The final checks executed real Chromium and the actual app in the pinned browser
container. Acceptance bodies stayed outside editable app files. All passed:

- JavaScript, Python and Bash syntax.
- Movement, reversal rejection, one turn per physics tick, pause, growth, score,
  wall and body collisions, restart, WASD, touch and mobile layout.
- Six completed rounds with five-game rollover, newest-first order, no duplicate
  results, high score, reload and reopening persistence, corrupt JSON recovery,
  and denied/quota storage with visible session-only notice.
- Clean-environment Bash startup from another working directory, own-server reuse,
  public-file allowlist and clean shutdown.

Additional laptop browser QA verified real-time movement, pause, fullscreen/escape,
1280×720 start-button visibility, mobile 390px layout and saved records after reload.
A deterministic-food Hamiltonian play test filled all 400 cells through real input
and physics and verified a won result, 3,970 points, stored best 3,970 and history 3,970.
There were zero browser console errors in the final browser session.

Fresh-terminal QA used only `HOME`, `LANG` and `PATH=/usr/bin:/bin`, with no venv or
Node environment. It verified the home-folder Bash command, Python discovery,
startup, repeated launch, clean Ctrl+C, app paths containing spaces, unrelated CWD,
foreign-port preservation, and the plain command in a fresh login shell.

An independent local-model advisory review was parsed by a strict report schema.
Its original report is preserved, including speculative/inaccurate findings.
Evidence-based adjudication is separate: the claimed win-history bug contradicted
both source and a full-board browser test, the input race was denied by the pending
turn guard and real-browser test, viewport units were supported on the tested
browser, and corrupt-data recovery matched the contract. No model prose silently
became runtime authority.

Framework regression: **3,001 passed, 67 skipped, 91% coverage**. The first parallel
attempt collided with the intentionally exclusive model lease; the complete suite
passed after the model job finished. Targeted schema/control checks, Ruff, Pyright
and import contracts are also retained. The final app's bytes match the frozen
browser verification and review manifests at publication.

## Evidence

Under `.loopforge/tasks/snake/`:

- `delivery-report.json`: published file hashes, launchers, checks and honest outcome.
- `assisted-verification.json` / `assisted-verified-files.json`: actual check outputs
  and verified app hashes.
- `fresh-terminal-verification.json`: fresh-terminal and process lifecycle evidence.
- `independent-advisory-review.json` / `review-resolution.json`: original advisory
  report and separate evidence-based adjudication.
- `verification-v4/`: final frozen browser contracts and hashes.
- `provider-diagnostics/` inside successor goals: bounded provider responses, with
  credentials excluded and restrictive file permissions.

Screenshots and game state are under `output/playwright/snake/`. Existing LoopForge
work, source checkpoints and production campaigns were preserved. No production
service was restarted and nothing was deployed externally.
