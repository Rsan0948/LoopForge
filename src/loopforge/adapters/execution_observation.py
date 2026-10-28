"""Bounded process observations, distinct from acceptance or tool failure."""

import json

from loopforge.ports.sandbox import SandboxCommandResult


def execution_observation(
    result: SandboxCommandResult, *, crash_markers: tuple[str, ...] = ()
) -> str:
    """A nonzero negative control is useful; an exception may leave partial output.

    These are observations only. Script output cannot manufacture check receipts.
    How an uncaught exception looks is language-specific, so the caller supplies
    ``crash_markers`` for its workloads; without them a nonzero exit stays a plain
    nonzero exit, never a guessed crash.
    """
    exception = result.exit_code != 0 and any(marker in result.stderr for marker in crash_markers)
    status = (
        "exception_partial_output"
        if exception
        else "nonzero_exit"
        if result.exit_code
        else "zero_exit"
    )
    return (
        "Execution observation (untrusted; not acceptance evidence):\n"
        + json.dumps(
            {
                "process_status": "exited",
                "script_status": status,
                "exit_code": result.exit_code,
                "measurement_status": "not_verified",
                "stdout_omitted_characters": max(0, len(result.stdout) - 4000),
                "stderr_omitted_characters": max(0, len(result.stderr) - 3000),
            },
            sort_keys=True,
        )
        + "\nA printed PASS is a script claim. Partial output before an exception does not "
        "establish that later stages ran. Nonzero exits can be intentional negative controls.\n"
        + f"exit_code={result.exit_code}\nstdout:\n{result.stdout[-4000:]}\n"
        + f"stderr:\n{result.stderr[-3000:]}"
    )
