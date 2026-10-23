"""Deterministic gold grading for locked constant-contract fixtures.

This deliberately narrow grader rejects unsupported claims. Real repairs are
qualified by a failing-baseline/passing-candidate regression, not this rubric.
"""

import ast
import re
from typing import cast

from loopforge.entrypoints.campaign_models import ReviewReport

WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "seven": "7"}


def normalized(text: str) -> str:
    for word, number in WORDS.items():
        text = re.sub(r"\b" + word + r"\b", number, text, flags=re.IGNORECASE)
    return text.casefold()


def grade_contract(report: ReviewReport | None, expected: bool, files: dict[str, str]) -> bool:  # noqa: PLR0911, PLR0912 - narrow gold rubric fails closed at each evidence boundary
    if report is None or report.unresolved:
        return False
    actionable = [f for f in report.findings if f.disposition != "rejected"]
    if not expected:
        return not actionable
    if len(actionable) != 1 or actionable[0].disposition != "suspected":
        return False
    finding = actionable[0]
    try:
        tree = ast.parse(files["impl.py"])
    except (KeyError, SyntaxError):
        return False
    contracts: list[tuple[str, str, str, str, str]] = []
    for node in tree.body:
        if (
            not isinstance(node, ast.Assign)
            or len(node.targets) != 1
            or not isinstance(node.targets[0], ast.Name)
        ):
            continue
        symbol = node.targets[0].id
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            continue
        if isinstance(value, list):
            value = len(cast("list[object]", value))
        if type(value) is not int:
            continue
        for path, text in files.items():
            if (
                not path.endswith(".md")
                or "historical" in text.lower()
                or "future proposal" in text.lower()
            ):
                continue
            for line in normalized(text).splitlines():
                if symbol.casefold() in line:
                    numbers = re.findall(r"\b\d+\b", line)
                    if len(numbers) == 1 and int(numbers[0]) != value:
                        contracts.append(
                            (
                                symbol,
                                numbers[0],
                                str(value),
                                path,
                                ast.get_source_segment(files["impl.py"], node) or "",
                            )
                        )
    for symbol, documented, actual, doc_path, assignment in contracts:
        claim = normalized(finding.title + " " + finding.explanation)
        if symbol.casefold() not in claim or not all(
            re.search(r"\b" + v + r"\b", claim) for v in (documented, actual)
        ):
            continue
        if "expected:" in claim and "observed:" in claim:
            expected_claim = claim.split("expected:", 1)[1].split("observed:", 1)[0]
            observed_claim = claim.split("observed:", 1)[1]
            if not re.search(r"\b" + documented + r"\b", expected_claim) or (
                not re.search(r"\b" + actual + r"\b", observed_claim)
                and normalized(assignment) not in observed_claim
            ):
                continue
        else:
            # Legacy free prose is insufficient to grade numeric direction.
            continue
        doc_ok = impl_ok = False
        for evidence in finding.evidence:
            if evidence.path not in files or evidence.quote not in files[evidence.path]:
                return False
            quote = normalized(evidence.quote)
            if symbol.casefold() not in quote:
                continue
            doc_ok |= evidence.path == doc_path and bool(
                re.search(r"\b" + documented + r"\b", quote)
            )
            impl_ok |= evidence.path == "impl.py" and (
                bool(re.search(r"\b" + actual + r"\b", quote))
                or (bool(assignment) and normalized(assignment) in quote)
            )
        if doc_ok and impl_ok:
            return True
    return False
