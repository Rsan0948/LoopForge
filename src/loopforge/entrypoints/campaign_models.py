"""Strict operator requests and evidence-only model reports for the campaign harness."""

import hashlib
import re
from pathlib import PurePosixPath
from typing import Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class DispatchRequest(StrictRecord):
    unit_key: str | None = Field(default=None, max_length=120, pattern=r"^[a-zA-Z0-9_:-]+$")
    dispatch_key: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_:-]+$")


class CampaignConfig(StrictRecord):
    repository: str
    project: Literal["brook", "helicops-cloud", "snake", "reconciliation"]
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    scope: list[str] = Field(min_length=1, max_length=30)
    rounds: int = Field(default=3, ge=1, le=20)
    model: Literal["qwen3.8:27b-q4_K_M", "qwen3.6:35b-a3b"] = "qwen3.6:35b-a3b"
    inference_profile: Literal[
        "baseline-v1",
        "qwen-coding-v1",
        "qwen-bounded-v2",
        "qwen-bounded-v3",
        "qwen-app-v1",
        "qwen-state-v1",
        "qwen-session-v1",
        "qwen-native-v1",
    ] = "baseline-v1"
    # Local inference engine for the coding worker. MLX serves the same native protocol
    # through adapters.mlx_transport; endpoint, model revision and renderer are code-owned.
    engine: Literal["ollama", "mlx"] = "ollama"
    max_iterations: int = Field(default=96, ge=2, le=192)
    max_tokens: int = Field(default=500_000, ge=1000, le=2_000_000)
    max_seconds: int = Field(default=7200, ge=30, le=14_400)
    model_timeout_seconds: int = Field(default=600, ge=30, le=1200)
    review_provider: Literal["local", "gemini"] = "local"
    repair_escalation: bool = False
    repair_budget_microusd: int = Field(default=500_000, ge=1, le=500_000)
    repair_recovery_run_id: str | None = Field(default=None, pattern=r"^run_[a-z0-9]{1,64}$")
    repair_decisions: bool = False
    force_repair_attempt: bool = False
    revisable_repair_decisions: bool = False

    @model_serializer(mode="wrap")
    def serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast("dict[str, object]", handler(self))
        if not self.revisable_repair_decisions:
            data.pop("revisable_repair_decisions", None)
        if not self.force_repair_attempt:
            data.pop("force_repair_attempt", None)
        if self.review_provider == "local":
            data.pop("review_provider", None)
        if self.engine == "ollama":
            data.pop("engine", None)
        if not self.repair_decisions:
            data.pop("repair_decisions", None)
        if self.repair_recovery_run_id is None:
            data.pop("repair_recovery_run_id", None)
        if not self.repair_escalation:
            # Preserve canonical authority hashes of pre-escalation intakes.
            data.pop("repair_escalation", None)
            data.pop("repair_budget_microusd", None)
        return data

    @model_validator(mode="after")
    def safe_scope(self) -> "CampaignConfig":
        if self.engine == "mlx" and (
            self.model != "qwen3.6:35b-a3b" or self.inference_profile != "qwen-native-v1"
        ):
            msg = "The MLX engine serves only the native qwen3.6:35b-a3b profile"
            raise ValueError(msg)
        if self.repair_escalation and self.inference_profile != "qwen-state-v1":
            msg = "Repair escalation requires the fresh-state profile"
            raise ValueError(msg)
        if self.repair_recovery_run_id is not None and not self.repair_escalation:
            msg = "A recovery reference requires frozen repair escalation authority"
            raise ValueError(msg)
        if self.repair_decisions and not self.repair_escalation:
            msg = "Repair decisions require frozen repair escalation authority"
            raise ValueError(msg)
        if self.revisable_repair_decisions and not self.repair_decisions:
            msg = "Revisable decisions require frozen repair decision authority"
            raise ValueError(msg)
        if self.force_repair_attempt and not self.repair_decisions:
            msg = "Forced repair attempts require frozen repair decision authority"
            raise ValueError(msg)
        for prefix in self.scope:
            if not prefix or prefix.startswith("/") or ".." in prefix.split("/"):
                msg = "scope must contain relative prefixes without parent traversal"
                raise ValueError(msg)
        return self


class Evidence(StrictRecord):
    path: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=1000)


class Finding(StrictRecord):
    title: str = Field(min_length=1, max_length=300)
    disposition: Literal["suspected", "rejected", "uncertain", "planned_gap"]
    severity: Literal["high", "medium", "low"]
    explanation: str = Field(min_length=1, max_length=2000)
    evidence: list[Evidence] = Field(min_length=1, max_length=10)
    next_check: str = Field(min_length=1, max_length=1000)


class ReviewReport(StrictRecord):
    summary: str = Field(min_length=1, max_length=2000)
    findings: list[Finding] = Field(max_length=40)
    unresolved: list[str] = Field(max_length=40)


class SourceReference(StrictRecord):
    excerpt_id: str = Field(pattern=r"^e[1-9][0-9]*$")
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote_start: int | None = Field(default=None, ge=0)
    quote_length: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def ordered(self) -> "SourceReference":
        if self.end_line < self.start_line:
            msg = "evidence line range must be ordered"
            raise ValueError(msg)
        if (self.quote_start is None) != (self.quote_length is None) or (
            self.quote_start is not None and self.start_line != self.end_line
        ):
            msg = "Column selection requires both fields and exactly one source line"
            raise ValueError(msg)
        return self


class ReferencedFindingFields(StrictRecord):
    title: str = Field(min_length=1, max_length=160)
    disposition: Literal["suspected", "rejected", "uncertain", "planned_gap"]
    severity: Literal["high", "medium", "low"]
    explanation: str = Field(min_length=1, max_length=800)
    next_check: str = Field(min_length=1, max_length=400)


class ReferencedFinding(ReferencedFindingFields):
    explanation: str = Field(min_length=1, max_length=2000)
    evidence: list[SourceReference] = Field(min_length=1, max_length=2)


class ReferencedReport(StrictRecord):
    """Small model submission; source quotations are materialized by trusted code."""

    summary: str = Field(min_length=1, max_length=600)
    findings: list[ReferencedFinding] = Field(max_length=2)
    unresolved: list[str] = Field(max_length=12)


class ShortSourceReference(StrictRecord):
    excerpt_id: str = Field(pattern=r"^e[1-9][0-9]*$")
    start_line: int = Field(ge=1)
    line_count: int = Field(ge=1, le=3)
    quote_start: int | None = Field(default=None, ge=0)
    quote_length: int | None = Field(default=None, ge=1, le=1000)


def require_provider_defect_basis(schema: dict[str, JsonValue]) -> None:
    """Provider generation must include fields conditionally enforced at runtime."""
    required = cast("list[str]", schema.setdefault("required", []))
    required.extend(
        name for name in ("expected_behavior", "observed_behavior") if name not in required
    )


def scalar_contract(text: str) -> tuple[str, str] | None:
    """Recognize only a single explicitly assigned numeric constant, without units."""
    # Narrative wrappers are allowed, but a conditional, negation, comparison,
    # extra number/symbol or trailing unit makes this an ambiguous claim.
    if re.search(
        r"\b(?:not|never|no|unless|if|when|minimum|maximum|least|most|"
        r"less|greater|before|after|either|and|or|but)\b",
        text,
        re.IGNORECASE,
    ):
        return None
    names = set(re.findall(r"(?<![\w])([A-Z][A-Z0-9_]+)(?![\w])", text))
    if len(names) != 1:
        return None
    match = re.search(
        r"`?([A-Z][A-Z0-9_]+)`?(?:\s+in\s+`?[\w./-]+`?)?\s*"
        r"(?:=|is(?: configured as)?|should be|as|to)\s*(\d+)"
        r"(?:\s+in\s+`?[\w/-]+\.[\w.-]+`?)?\s*[.]?\s*$",
        text,
    )
    if not match or re.search(r"\b\d+\b", text[: match.start()]):
        return None
    return match[1], str(int(match[2]))


class ShortReferencedFinding(ReferencedFindingFields):
    model_config = ConfigDict(json_schema_extra=require_provider_defect_basis)
    evidence: list[ShortSourceReference] = Field(min_length=1, max_length=2)
    expected_behavior: str = Field(
        default="",
        max_length=300,
        description="Current documentation or normative contract claim; not a proposed repair",
    )
    observed_behavior: str = Field(
        default="",
        max_length=300,
        description="Actual implementation fact, distinct from the documented expectation",
    )

    @model_validator(mode="after")
    def defect_basis(self) -> "ShortReferencedFinding":
        if self.disposition != "suspected":
            return self
        if not self.expected_behavior.strip() or not self.observed_behavior.strip():
            msg = (
                "Suspected defects require expected/observed behavior; "
                "descriptions go in observations"
            )
            raise ValueError(msg)
        if self.expected_behavior.strip().casefold() == self.observed_behavior.strip().casefold():
            msg = "Expected and observed agree; move descriptive consistency to observations"
            raise ValueError(msg)
        expected_scalar = scalar_contract(self.expected_behavior)
        if expected_scalar is not None and expected_scalar == scalar_contract(
            self.observed_behavior
        ):
            msg = (
                "Expected and observed assign the same scalar constant; "
                "use observations, not a suspected defect"
            )
            raise ValueError(msg)
        if len({e.model_dump_json() for e in self.evidence}) < 2:
            msg = (
                "Suspected defects require expected/observed behavior and two distinct source "
                "selections: expected contract and contrary actual behavior. A working "
                "validation/authorization denial belongs in observations, not suspected defects."
            )
            raise ValueError(msg)
        return self


class ShortReferencedReport(StrictRecord):
    """Reporter-only bounded selections; original source validation still follows."""

    summary: str = Field(min_length=1, max_length=600)
    findings: list[ShortReferencedFinding] = Field(max_length=2)
    unresolved: list[str] = Field(max_length=6)
    observations: list[str] = Field(default_factory=list, max_length=6)
    overflow_checks: list[str] = Field(default_factory=list, max_length=6)

    def referenced(self) -> ReferencedReport:
        data = self.model_dump()
        data.pop("observations")
        data.pop("overflow_checks")
        data["unresolved"] = [*self.unresolved, *self.overflow_checks]
        for finding, source in zip(data["findings"], self.findings, strict=True):
            expected = finding.pop("expected_behavior")
            observed = finding.pop("observed_behavior")
            if expected or observed:
                finding["explanation"] = (
                    str(finding["explanation"])
                    + "\nExpected: "
                    + str(expected)
                    + "\nObserved: "
                    + str(observed)
                )
            finding["evidence"] = [
                {
                    "excerpt_id": span.excerpt_id,
                    "start_line": span.start_line,
                    "end_line": span.start_line + span.line_count - 1,
                    "quote_start": span.quote_start,
                    "quote_length": span.quote_length,
                }
                for span in source.evidence
            ]
        return ReferencedReport.model_validate(data)


class OperatorCommand(StrictRecord):
    command: Literal["pause", "cancel", "resume", "advance", "complete", "model_changed", "park"]
    evidence: str = Field(min_length=1, max_length=4000)
    branch: str | None = None
    model: Literal["qwen3.8:27b-q4_K_M", "qwen3.6:35b-a3b"] | None = None

    @model_validator(mode="after")
    def model_command(self) -> "OperatorCommand":
        if (self.command == "model_changed") != (self.model is not None):
            msg = "model is required only for model_changed"
            raise ValueError(msg)
        return self


class CodingCheck(StrictRecord):
    reusable: bool = False
    name: str = Field(pattern=r"^check_[a-z][a-z0-9_]{0,40}$")
    argv: list[str] = Field(min_length=1, max_length=30)
    timeout_seconds: int = Field(default=120, ge=1, le=600)


class AcceptanceProbe(StrictRecord):
    """Operator-frozen independent Python check; its body is outside editable files."""

    criterion_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,40}$")
    description: str = Field(min_length=10, max_length=1000)
    name: str = Field(pattern=r"^check_acceptance_[a-z][a-z0-9_]{0,24}$")
    executable: str = Field(default="/usr/local/bin/python", min_length=1, max_length=300)
    source: str = Field(min_length=10, max_length=16000)
    timeout_seconds: int = Field(default=120, ge=1, le=600)

    @model_validator(mode="after")
    def absolute_executable(self) -> "AcceptanceProbe":
        if (
            not PurePosixPath(self.executable).is_absolute()
            or ".." in PurePosixPath(self.executable).parts
        ):
            msg = "acceptance probes require an absolute in-container Python executable"
            raise ValueError(msg)
        return self

    def check(self) -> CodingCheck:
        return CodingCheck(
            name=self.name,
            argv=[
                self.executable,
                "-I",
                "-B",
                "-c",
                (
                    f"exec(compile({self.source!r}, '<trusted-acceptance>', 'exec'), "
                    "{'__name__': '__main__'})\n"
                    f"print({self.completion_marker!r}, flush=True)\n"
                ),
            ],
            timeout_seconds=self.timeout_seconds,
            reusable=False,
        )

    @property
    def completion_marker(self) -> str:
        identity = f"{self.criterion_id}\0{self.name}\0{self.source}"
        return "LOOPFORGE_ACCEPTANCE_COMPLETE:" + hashlib.sha256(identity.encode()).hexdigest()


class CodingTaskConfig(StrictRecord):
    objective: str = Field(min_length=10, max_length=8000)
    kind: Literal["bugfix", "launch", "feature"]
    branch: str = Field(min_length=1, max_length=100)
    allowed_paths: list[str] = Field(min_length=1, max_length=30)
    editable_project_paths: list[str] = Field(default_factory=list, max_length=30)
    immutable_paths: list[str] = Field(default_factory=list, max_length=30)
    # Legacy terminal tasks load with an empty list; new admission requires
    # explicit, immutable deliverables before model execution.
    required_changed_paths: list[str] = Field(default_factory=list, max_length=30)
    image: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    checks: list[CodingCheck] = Field(min_length=1, max_length=10)
    acceptance_probes: list[AcceptanceProbe] = Field(
        default_factory=list[AcceptanceProbe], max_length=10
    )
    evidence: str = Field(min_length=1, max_length=4000)
    max_attempts: int = Field(default=5, ge=1, le=5)
    max_changed_files: int = Field(default=12, ge=1, le=40)
    max_review_attempts: int = Field(default=3, ge=1, le=5)
    max_task_tokens: int = Field(default=2_000_000, ge=1000, le=5_000_000)
    max_task_seconds: int = Field(default=21_600, ge=30, le=43_200)
    max_review_reads: int = Field(default=64, ge=2, le=256)
    # Operator-frozen container descriptor ceiling. Browser workloads (Chromium
    # plus an app server) stall under the legacy 128. Repository or model
    # content can never set this; it is part of the frozen task contract.
    sandbox_max_open_files: int = Field(default=128, ge=64, le=4096)
    # Operator-frozen review termination: tool calls allowed after required
    # coverage is complete, then only submit_report. None keeps legacy behavior.
    max_review_calls_after_coverage: int | None = Field(default=None, ge=1, le=20)

    @model_serializer(mode="wrap")
    def serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast("dict[str, object]", handler(self))
        for name in ("editable_project_paths", "immutable_paths"):
            if not getattr(self, name):
                data.pop(name, None)
        # Omit the legacy default so previously frozen contract hashes are unchanged.
        if self.sandbox_max_open_files == 128:
            data.pop("sandbox_max_open_files", None)
        if self.max_review_calls_after_coverage is None:
            data.pop("max_review_calls_after_coverage", None)
        return data

    @model_validator(mode="after")
    def contract(self) -> "CodingTaskConfig":
        if self.max_review_reads <= self.max_changed_files:
            msg = "review allowance must cover changed files plus diff"
            raise ValueError(msg)
        for approved in self.editable_project_paths:
            parts = PurePosixPath(approved).parts
            if (
                not parts
                or approved.startswith("/")
                or ".." in parts
                or any(p in {".git", ".env", "secrets", "credentials"} for p in parts)
                or any(p.startswith(".env.") or p == ".ssh" for p in parts)
                or PurePosixPath(approved).suffix.lower()
                in {".pem", ".key", ".p12", ".pfx", ".der"}
                or PurePosixPath(approved).name in {"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}
                or approved in self.immutable_paths
                or not any(
                    approved == scope or approved.startswith(scope.rstrip("/") + "/")
                    for scope in self.allowed_paths
                )
            ):
                msg_0 = (
                    "Editable project files require explicit scope "
                    "and cannot include trusted or secret files"
                )
                raise ValueError(msg_0)
        for prefix in self.allowed_paths:
            path = PurePosixPath(prefix)
            if (
                not prefix
                or path.is_absolute()
                or ".." in path.parts
                or prefix == "."
                or (
                    any(part.startswith(".") for part in path.parts)
                    and prefix not in self.editable_project_paths
                )
            ):
                msg = "coding paths must be explicit relative paths without hidden components"
                raise ValueError(msg)
        if len(self.required_changed_paths) != len(set(self.required_changed_paths)):
            msg = "required changed paths must be unique"
            raise ValueError(msg)
        for name in self.required_changed_paths:
            path = PurePosixPath(name)
            if (
                not name
                or path.is_absolute()
                or ".." in path.parts
                or any(part.startswith(".") for part in path.parts)
                or not any(
                    name == prefix or name.startswith(prefix.rstrip("/") + "/")
                    for prefix in self.allowed_paths
                )
            ):
                msg = "required changed paths must be explicit files within allowed paths"
                raise ValueError(msg)
        ids = [probe.criterion_id for probe in self.acceptance_probes]
        if len(ids) != len(set(ids)):
            msg = "acceptance criterion ids must be unique"
            raise ValueError(msg)
        names = [check.name for check in self.execution_checks]
        if len(names) != len(set(names)):
            msg = "check names must be unique"
            raise ValueError(msg)
        if any(not check.argv[0].startswith("/") for check in self.checks):
            msg = "checks must name an absolute in-container executable"
            raise ValueError(msg)
        return self

    @property
    def execution_checks(self) -> tuple[CodingCheck, ...]:
        return (*self.checks, *(probe.check() for probe in self.acceptance_probes))


class CodingCommand(StrictRecord):
    command: Literal["retry", "accept", "reject", "request_changes"]
    evidence: str = Field(min_length=1, max_length=4000)
    prepared_patch_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def prepared_patch_only_on_retry(self) -> "CodingCommand":
        if self.prepared_patch_sha256 is not None and self.command != "retry":
            msg = "prepared patch identity is permitted only on retry"
            raise ValueError(msg)
        return self


class ReviewCorrection(StrictRecord):
    failed_run_id: str = Field(min_length=1, max_length=120)
    implementation: str = Field(pattern=r"^[a-f0-9]{64}$")
    failure_code: str = Field(min_length=1, max_length=200)
    evidence: str = Field(min_length=20, max_length=4000)
    validated_checks: list[str] = Field(min_length=1, max_length=20)
