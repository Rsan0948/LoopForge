"""Every rule that can end a worker or reviewer run, listed in one place.

Found by reading the source, so a new stop rule cannot be added without appearing here
and being looked at as part of the whole set. Live runs kept finding stop rules one at a
time; this inventory is what the offline trajectory suite has to cover.
"""

import ast
from pathlib import Path

import loopforge
from loopforge.application.failure_recovery import reason_kind
from loopforge.domain.investigation import FailureKind

SOURCE = Path(loopforge.__file__).parent

# Runtime control policy: budgets and progress.
CONTROL_STOPS = {
    "STOP_SUCCESS_VERIFIED": "verified success",
    "STOP_BUDGET_EXHAUSTED": "hard budget: cost",
    "STOP_TOKEN_BUDGET_EXHAUSTED": "hard budget: work tokens (input not served from the "
    "provider's prompt cache, plus output)",
    "STOP_TIME_BUDGET_EXHAUSTED": "hard budget: wall clock",
    "STOP_STALLED_NO_PROGRESS": "N verifications without a better score (F2)",
    "STOP_MAX_ITERATIONS": "hard budget: iterations",
}

# Workload rules raised from model wrappers: as a typed gate stop (stalled or failed,
# recorded honestly) or, for the reviewer still, as a permanent model failure.
WRAPPER_STOPS = {
    "CODING_REQUIRED_CHECK_CIRCUIT_OPEN": "a required check failed to run N times "
    "(infrastructure; a check that ran and failed is a measured result)",
    "CODING_AUTOMATIC_CHECK_BLOCKED": "checks could not run (inconclusive), or the frozen "
    "contract changed",
    "CODING_NO_NEW_EVIDENCE": "N turns with an unchanged patch and no new tool result, "
    "then one warning, then N more: stalled",
    "MODEL_DEADLINE": "run deadline reached before a model call",
    "REVIEW_NO_NEW_EVIDENCE": "local reviewer: six turns without new source",
    "REVIEW_REPEATING": "reviewer tool results repeat N times, then a warning, then N more: "
    "stalled (replaces counting refused reviewer input toward circuits)",
    "REVIEW_SOURCE_CIRCUIT_OPEN": "reviewer source tool circuit opened",
    "REVIEW_REPORT_CIRCUIT_OPEN": "reviewer report tool circuit opened",
    "REVIEW_REQUIRED_READ_LIMIT": "reviewer read capacity exhausted",
    "REVIEW_REQUIRED_EVIDENCE_INVALID": "required review evidence invalid",
    "REVIEW_REPAIR_REQUIRED": "review report needs repair",
    "REPORT_EVIDENCE_REQUIRED": "report lacks required evidence",
    "REPORT_PACKET_LIMIT": "report exceeds packet limit",
    "REPORT_PREFLIGHT_SOURCE": "report preflight source invalid",
    "REPORT_REMAINING_ALLOWANCE": "report allowance exhausted",
}


def _literal(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _calls(path: Path, name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text())
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def test_every_control_stop_is_inventoried() -> None:
    found = {
        code
        for call in _calls(SOURCE / "domain" / "policy.py", "ControlDecision")
        if len(call.args) > 1 and (code := _literal(call.args[1])) and code.startswith("STOP_")
    }
    assert found == set(CONTROL_STOPS)


def test_every_permanent_wrapper_stop_is_inventoried() -> None:
    found: set[str] = set()
    for path in (SOURCE / "entrypoints").glob("*.py"):
        for call in _calls(path, "RunGateStop"):
            if len(call.args) > 1 and (code := _literal(call.args[1])):
                found.add(code)
        for call in _calls(path, "ModelTurnError"):
            if (
                len(call.args) > 1
                and isinstance(call.args[0], ast.Attribute)
                and call.args[0].attr == "PERMANENT"
                and (code := _literal(call.args[1]))
            ):
                found.add(code)
    assert found == set(WRAPPER_STOPS)


def test_every_literal_reason_code_has_an_explicit_failure_kind() -> None:
    codes: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        for name in ("ModelTurnError", "RunGateStop"):
            for call in _calls(path, name):
                if len(call.args) < 2:
                    continue
                argument = call.args[1]
                # A code chosen by a conditional, e.g. 'A' if x else 'B'.
                options = (
                    (argument.body, argument.orelse)
                    if isinstance(argument, ast.IfExp)
                    else (argument,)
                )
                codes.update(code for option in options if (code := _literal(option)))
    unmapped = sorted(code for code in codes if reason_kind(code) is FailureKind.UNKNOWN)
    assert unmapped == []
