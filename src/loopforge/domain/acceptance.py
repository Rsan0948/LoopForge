"""Revision-bound acceptance evidence; model completion claims are not receipts."""

from dataclasses import dataclass


def _hash(value: str, name: str, length: int = 64) -> None:
    if (
        not isinstance(value, str)  # pyright: ignore[reportUnnecessaryIsInstance]
        or len(value) != length
        or any(char not in "0123456789abcdef" for char in value)
    ):
        msg = f"{name} must be a lowercase hexadecimal digest"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateRevision:
    base_revision: str
    patch_sha256: str

    def __post_init__(self) -> None:
        _hash(self.base_revision, "base revision", 40)
        _hash(self.patch_sha256, "candidate patch")


@dataclass(frozen=True, slots=True, kw_only=True)
class CriterionEvidence:
    criterion_id: str
    check_name: str
    passed: bool
    executed: bool

    def __post_init__(self) -> None:
        if not self.criterion_id.strip() or not self.check_name.strip():
            msg = "acceptance criterion and check identities cannot be empty"
            raise ValueError(msg)
        if type(self.passed) is not bool or type(self.executed) is not bool:
            msg = "acceptance verdicts must be booleans"
            raise TypeError(msg)
        if self.passed and not self.executed:
            msg = "unexecuted acceptance criteria cannot pass"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptanceReceipt:
    candidate: CandidateRevision
    contract_sha256: str
    run_id: str
    criteria: tuple[CriterionEvidence, ...]

    def __post_init__(self) -> None:
        _hash(self.contract_sha256, "acceptance contract")
        if not isinstance(self.candidate, CandidateRevision):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "acceptance requires a candidate revision"
            raise TypeError(msg)
        if not isinstance(self.run_id, str) or not self.run_id.strip():  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "acceptance requires a verification run"
            raise ValueError(msg)
        if (
            type(self.criteria) is not tuple
            or not self.criteria
            or any(
                not isinstance(item, CriterionEvidence)  # pyright: ignore[reportUnnecessaryIsInstance]
                for item in self.criteria
            )
        ):
            msg = "acceptance requires immutable criterion evidence"
            raise ValueError(msg)
        ids = [item.criterion_id for item in self.criteria]
        names = [item.check_name for item in self.criteria]
        if len(set(ids)) != len(ids) or len(set(names)) != len(names):
            msg = "acceptance criterion and check identities must be unique"
            raise ValueError(msg)

    def authorizes(
        self,
        *,
        candidate: CandidateRevision,
        contract_sha256: str,
        required: tuple[tuple[str, str], ...],
        run_id: str,
    ) -> bool:
        """Only the exact authorized contract, revision and complete check set qualify."""
        return (
            self.candidate == candidate
            and self.contract_sha256 == contract_sha256
            and self.run_id == run_id
            and bool(required)
            and len(set(required)) == len(required)
            and {(item.criterion_id, item.check_name) for item in self.criteria} == set(required)
            and all(item.passed and item.executed for item in self.criteria)
        )
