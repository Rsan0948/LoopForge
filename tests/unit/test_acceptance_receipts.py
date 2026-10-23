"""Independent acceptance cannot be satisfied by editable tests or stale receipts."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from loopforge.adapters.acceptance_receipts import decode_acceptance, encode_acceptance
from loopforge.adapters.git_workspace import GitWorkspace, GitWorkspaceManager
from loopforge.domain.acceptance import AcceptanceReceipt, CandidateRevision, CriterionEvidence
from loopforge.domain.state import RunState
from loopforge.domain.types import RunId
from loopforge.domain.verification import CheckOutcome
from loopforge.domain.workspace import FixtureFile, FixtureSpec
from loopforge.entrypoints.campaign_coding import (
    CodingVerifier,
    acceptance_contract_hash,
    acceptance_requirements,
    candidate_revision,
    require_acceptance_detail,
)
from loopforge.entrypoints.campaign_models import AcceptanceProbe, CodingCheck, CodingTaskConfig
from loopforge.ports.verifier import VerificationResult


def task() -> CodingTaskConfig:
    return CodingTaskConfig(
        objective="Fix addition using independent numeric acceptance",
        kind="bugfix",
        branch="test/acceptance",
        image="sha256:" + "a" * 64,
        allowed_paths=["src"],
        required_changed_paths=["src/add.py"],
        evidence="failing arithmetic",
        checks=[CodingCheck(name="check_project", argv=["/usr/local/bin/python", "-c", "pass"])],
        acceptance_probes=[
            AcceptanceProbe(
                criterion_id="numeric_addition",
                description="Addition must satisfy the numeric contract",
                name="check_acceptance_addition",
                source="assert 1 + 2 == 3",
            )
        ],
    )


def workspace(root: Path) -> GitWorkspace:
    return GitWorkspaceManager(root).materialize(
        FixtureSpec(
            fixture_id="acceptance",
            files=(
                FixtureFile(path="src/add.py", content="def add(a,b): return 0\n"),
                FixtureFile(path="src/add.test.py", content="assert True\n"),
            ),
        )
    )


class BehaviorVerifier:
    """Test oracle independent of the editable project-test content; no execution."""

    def __init__(self, workspace: GitWorkspace) -> None:
        self.workspace = workspace
        self.calls = 0

    def verify(self, state: RunState) -> VerificationResult:
        del state
        self.calls += 1
        behavior_passes = "return a+b" in (self.workspace.root / "src/add.py").read_text()
        outcomes = (
            CheckOutcome(name="command:check_project", passed=True, detail="editable tests green"),
            CheckOutcome(
                name="command:check_acceptance_addition",
                passed=behavior_passes,
                detail="independent oracle",
            ),
        )
        return VerificationResult(
            behavior_passes, "independent behavior measured", outcomes=outcomes
        )


def test_green_project_tests_cannot_accept_the_wrong_implementation(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws.root / "src/add.py").write_text("def add(a,b): return -1\n")
    (ws.root / "src/add.test.py").write_text("assert True  # still green, but misleading\n")
    config = task()
    verifier = CodingVerifier(BehaviorVerifier(ws), config, ws)
    state = RunState(RunId("verification"))
    assert not verifier.verify(state).passed
    receipt = verifier.acceptance_receipt
    assert receipt is not None
    assert not receipt.authorizes(
        candidate=candidate_revision(ws),
        contract_sha256=acceptance_contract_hash(config),
        required=acceptance_requirements(config),
        run_id="verification",
    )
    (ws.root / "src/add.py").write_text("def add(a,b): return a+b\n")
    assert verifier.verify(state).passed
    receipt = verifier.acceptance_receipt
    assert receipt is not None
    detail: dict[str, object] = {
        "run_id": "verification",
        "acceptance_receipt": json.loads(encode_acceptance(receipt)),
    }
    assert require_acceptance_detail(config, ws, detail) == receipt
    # A candidate edit, even with the same base commit and unchanged green tests,
    # invalidates the receipt.
    (ws.root / "src/add.py").write_text("def add(a,b): return a-b\n")
    with pytest.raises(ValueError, match="stale"):
        require_acceptance_detail(config, ws, detail)


def test_candidate_changes_during_verification_cannot_produce_a_receipt(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws.root / "src/add.py").write_text("def add(a,b): return a+b\n")

    class MutatingVerifier(BehaviorVerifier):
        def verify(self, state: RunState) -> VerificationResult:
            result = super().verify(state)
            (self.workspace.root / "src/add.py").write_text("def add(a,b): return a-b\n")
            return result

    verifier = CodingVerifier(MutatingVerifier(ws), task(), ws)
    result = verifier.verify(RunState(RunId("race")))
    assert not result.passed
    assert result.inconclusive
    assert verifier.acceptance_receipt is None


def test_frozen_contract_cannot_be_mutated_between_checks(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws.root / "src/add.py").write_text("def add(a,b): return a+b\n")
    config = task()
    measured = BehaviorVerifier(ws)
    verifier = CodingVerifier(measured, config, ws)
    config.acceptance_probes.clear()
    result = verifier.verify(RunState(RunId("mutated-contract")))
    assert not result.passed
    assert result.inconclusive
    assert measured.calls == 0


def test_prose_only_success_cannot_replace_typed_acceptance_outcomes(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws.root / "src/add.py").write_text("def add(a,b): return a+b\n")

    class ProseVerifier:
        def verify(self, state: RunState) -> VerificationResult:
            del state
            return VerificationResult(True, "check_acceptance_addition: passed")

    result = CodingVerifier(ProseVerifier(), task(), ws).verify(RunState(RunId("prose")))
    assert not result.passed
    assert result.inconclusive


def receipt() -> AcceptanceReceipt:
    return AcceptanceReceipt(
        candidate=CandidateRevision(base_revision="a" * 40, patch_sha256="b" * 64),
        contract_sha256="c" * 64,
        run_id="fresh-review",
        criteria=(
            CriterionEvidence(
                criterion_id="behavior", check_name="check_behavior", passed=True, executed=True
            ),
        ),
    )


@pytest.mark.parametrize(
    "change", ["base", "patch", "contract", "run", "criterion", "check", "failed", "unexecuted"]
)
def test_receipt_denies_each_mismatched_or_failed_obligation(change: str) -> None:
    original = receipt()
    candidate = original.candidate
    contract = original.contract_sha256
    run = original.run_id
    required = (("behavior", "check_behavior"),)
    tested = original
    if change == "base":
        candidate = replace(candidate, base_revision="d" * 40)
    elif change == "patch":
        candidate = replace(candidate, patch_sha256="e" * 64)
    elif change == "contract":
        contract = "f" * 64
    elif change == "run":
        run = "another-run"
    elif change == "criterion":
        required = (("other", "check_behavior"),)
    elif change == "check":
        required = (("behavior", "check_other"),)
    else:
        tested = replace(
            original,
            criteria=(
                replace(original.criteria[0], passed=False, executed=change != "unexecuted"),
            ),
        )
    assert not tested.authorizes(
        candidate=candidate, contract_sha256=contract, required=required, run_id=run
    )


def test_receipt_round_trip_and_extra_authority_fields_are_strictly_validated() -> None:
    original = receipt()
    assert decode_acceptance(encode_acceptance(original)) == original
    payload = json.loads(encode_acceptance(original))
    payload["allow_merge"] = True
    with pytest.raises(ValidationError):
        decode_acceptance(json.dumps(payload))
    del payload["allow_merge"]
    payload["criteria"][0]["passed"] = "true"
    with pytest.raises(ValidationError):
        decode_acceptance(json.dumps(payload))


@pytest.mark.parametrize("failure", ["duplicate", "relative", "extra"])
def test_probe_contract_denies_invalid_definitions(failure: str) -> None:
    config = task().model_dump()
    if failure == "duplicate":
        config["acceptance_probes"] *= 2
    elif failure == "relative":
        config["acceptance_probes"][0]["executable"] = "python"
    else:
        config["acceptance_probes"][0]["grant_network"] = True
    with pytest.raises(ValidationError):
        CodingTaskConfig.model_validate(config)


def test_typed_check_evidence_cannot_be_contradicted_or_reclassified_as_unexecuted() -> None:
    failed = CheckOutcome(name="criterion", passed=False, detail="behavior fails")
    with pytest.raises(ValueError, match="contradict"):
        VerificationResult(True, "worker says success", outcomes=(failed,))
    with pytest.raises(TypeError, match="executed must be a bool"):
        CheckOutcome(name="criterion", passed=False, detail="unmeasured", executed="false")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unique"):
        VerificationResult(False, "duplicate", outcomes=(failed, failed))
