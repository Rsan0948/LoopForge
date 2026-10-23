"""Strict JSON persistence boundary for code-owned acceptance receipts."""

from dataclasses import asdict

from pydantic import BaseModel, ConfigDict

from loopforge.domain.acceptance import AcceptanceReceipt, CandidateRevision, CriterionEvidence


class _Candidate(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    base_revision: str
    patch_sha256: str


class _Criterion(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    criterion_id: str
    check_name: str
    passed: bool
    executed: bool


class _Receipt(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    candidate: _Candidate
    contract_sha256: str
    run_id: str
    criteria: list[_Criterion]


def encode_acceptance(receipt: AcceptanceReceipt) -> str:
    data = asdict(receipt)
    data["criteria"] = list(data["criteria"])
    return _Receipt.model_validate(data).model_dump_json()


def decode_acceptance(payload: str) -> AcceptanceReceipt:
    wire = _Receipt.model_validate_json(payload)
    return AcceptanceReceipt(
        candidate=CandidateRevision(**wire.candidate.model_dump()),
        contract_sha256=wire.contract_sha256,
        run_id=wire.run_id,
        criteria=tuple(CriterionEvidence(**item.model_dump()) for item in wire.criteria),
    )
