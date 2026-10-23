"""Opt-in bounded specialist turn through the ordinary action/verification pipeline."""

import difflib
import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

import httpx

from loopforge.adapters.gemini_repair import GeminiRepair, RepairDiagnosis
from loopforge.adapters.gemini_supervisor import load_gemini_key
from loopforge.adapters.repair_journal import RepairJournal
from loopforge.domain.actions import ActionProposal
from loopforge.domain.context import ContextItem, ContextSource, ModelContext, ModelRole
from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.events import (
    ActionProposed,
    ContextAssembled,
    Event,
    ModelAttemptFailed,
    ToolFailed,
    VerificationFailed,
    VerificationPassed,
)
from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.repair_escalation import (
    RepairReserved,
    RepairSample,
    RepairSettled,
    escalation_due,
    stall_escalation_due,
)
from loopforge.domain.routing import ModelCapabilities
from loopforge.domain.security import TrustClass
from loopforge.domain.state import RunState, replay
from loopforge.domain.tooling import (
    ApprovalClass,
    DataSensitivity,
    IdempotencyClass,
    RetryClass,
    SideEffectClass,
    ToolMetadata,
)
from loopforge.domain.types import (
    ActionId,
    ContextItemId,
    Permission,
    RiskLevel,
    RunId,
    RunStatus,
    UsageDelta,
)
from loopforge.entrypoints.campaign_evidence import source_allowed
from loopforge.entrypoints.campaign_models import CampaignConfig, CodingTaskConfig
from loopforge.entrypoints.campaign_progress import current_patch, stagnant_turns
from loopforge.ports.context import ContextBuilderPort
from loopforge.ports.model import ModelPort, ModelTurn
from loopforge.ports.paid_budget import PaidBudgetPort
from loopforge.ports.state_store import StateStorePort
from loopforge.ports.supervisor import SupervisorFailure
from loopforge.ports.tools import ToolExecutionRequest, ToolExecutorPort, ToolResult

FEEDBACK_TOOL = "record_repair_guidance"


def diagnostic_excerpt(text: str) -> str:
    """Retain exact error lines plus head/tail; full output is archived separately."""
    if len(text) <= 7600:
        return text
    selected: list[str] = []
    size = 0
    for line in text.splitlines():
        if re.search(
            r"(?:Error|Exception):|^E\s|^FAILED |^ERROR |AssertionError|/workspace/app/", line
        ):
            if size + len(line) > 4000:
                continue
            selected.append(line)
            size += len(line) + 1
    return (
        text[:1200]
        + "\n[exact error lines from full output]\n"
        + "\n".join(selected)
        + "\n[output tail; intervening text omitted]\n"
        + text[-1800:]
    )


def failure_identity(summary: str) -> str:
    """Ignore measurement noise, never expected/actual business values."""
    diagnostic = summary.split("Check receipts:", 1)[0]
    diagnostic = re.sub(r"\\+x0a", "\n", diagnostic)
    diagnostic = re.sub(
        r"(\b(?:in|took|elapsed|duration)[=:]?\s*)\d+(?:\.\d+)?\s*(?:seconds?|secs?|ms|s)\b",
        r"\1<duration>",
        diagnostic,
    )
    diagnostic = re.sub(
        r"(?m)^\d{4}-\d\d-\d\d[T ][\d:.]+(?:Z|[+-]\d\d:\d\d)?",
        "<time>",
        diagnostic,
    )
    diagnostic = re.sub(r"0x[0-9a-fA-F]+\b", "<address>", diagnostic)
    return hashlib.sha256(diagnostic.strip().encode()).hexdigest()


def failure_samples(events: Sequence[Event]) -> tuple[RepairSample, ...]:
    """Pair each failed verification with its next measured candidate snapshot."""
    samples: list[RepairSample] = []
    pending: str | None = None
    for event in events:
        if isinstance(event, VerificationPassed) and event.task_complete:
            samples, pending = [], None
        elif isinstance(event, VerificationFailed):
            pending = event.summary.split("\nCheck receipts:", 1)[0]
        elif isinstance(event, ContextAssembled) and pending is not None:
            packet = next(
                (i for i in event.context_items if str(i.item_id).endswith(":continuation-state")),
                None,
            )
            if packet:
                patch = json.loads(packet.content).get("current_patch_sha256")
                if isinstance(patch, str):
                    samples.append(RepairSample(failure_identity(pending), patch))
                    pending = None
    return tuple(samples)


def tiny_patch(diagnosis: RepairDiagnosis) -> bool:
    patch = diagnosis.patch
    if (
        patch is None
        or patch.old == patch.new
        or len(patch.old.encode()) + len(patch.new.encode()) > 2000
    ):
        return False
    return small_line_change(patch.old, patch.new)


def small_line_change(old: str, new: str) -> bool:
    """Count actual changed physical lines, including substring boundary effects."""
    removed = added = 0
    for line in difflib.ndiff(old.splitlines(keepends=True), new.splitlines(keepends=True)):
        removed += line.startswith("- ")
        added += line.startswith("+ ")
    return max(removed, added) <= 2


class RepairGuidanceTools:
    def __init__(
        self, delegate: ToolExecutorPort, journal: RepairJournal, checks: tuple[str, ...]
    ) -> None:
        self.delegate, self.journal, self.checks = delegate, journal, checks

    def metadata_for(self, tool_name: str) -> ToolMetadata:
        if tool_name != FEEDBACK_TOOL:
            return self.delegate.metadata_for(tool_name)
        return ToolMetadata(
            name=tool_name,
            risk=RiskLevel.READ_ONLY,
            required_permission=Permission.READ,
            side_effect=SideEffectClass.PURE,
            retry=RetryClass.NEVER,
            idempotency=IdempotencyClass.NOT_APPLICABLE,
            approval=ApprovalClass.NONE,
            timeout_seconds=5,
            sensitivity=DataSensitivity.INTERNAL,
        )

    def execute(self, request: ToolExecutionRequest) -> ToolResult:
        if request.proposal.tool_name != FEEDBACK_TOOL:
            result = self.delegate.execute(request)
            if request.proposal.tool_name not in self.checks:
                return result
            digest = hashlib.sha256(result.observation.encode()).hexdigest()
            archive = self.journal.path.parent / f"check-output-{digest}.txt"
            archive.write_text(result.observation)
            return replace(
                result,
                observation=diagnostic_excerpt(result.observation)
                + f"\nFull check output archived as check-output-{digest}.txt",
            )
        if request.proposal.arguments:
            return ToolResult(
                ok=False,
                observation="Guidance acknowledgement accepts no arguments",
                error_code="REPAIR_GUIDANCE_ARGUMENTS",
                failure_class=ToolFailureClass.PERMANENT,
            )
        # No model-authored content is executed: this action only acknowledges
        # durable advice, whose complete typed body is projected by RepairContext.
        return ToolResult(
            ok=True, observation="Specialist guidance is stored in current repair state."
        )


class RepairContext:
    def __init__(self, delegate: ContextBuilderPort, journal: RepairJournal) -> None:
        self.delegate, self.journal = delegate, journal

    def build_context(
        self,
        state: RunState,
        *,
        role: ModelRole = ModelRole.CONTROLLER,
        token_budget: ContextTokenBudget | None = None,
    ) -> ModelContext:
        context = self.delegate.build_context(state, role=role, token_budget=token_budget)
        guidance = self.journal.latest()
        if guidance is None:
            return context
        data = json.loads(guidance)
        current = next(
            (
                json.loads(i.content).get("current_patch_sha256")
                for i in context.items
                if str(i.item_id).endswith(":continuation-state")
            ),
            None,
        )
        if current is not None and data.get("basis_patch_sha256") != current:
            # Retain provenance without re-injecting obsolete executable suggestions.
            # The complete diagnosis remains in the immutable specialist ledger.
            data = {
                "basis_patch_sha256": data.get("basis_patch_sha256"),
                "current_patch_sha256": current,
                "current_applicability": "Historical advice for a different candidate; "
                "superseded instructions omitted. Use current source and verification.",
                "historical_advice_sha256": hashlib.sha256(guidance.encode()).hexdigest(),
                "verification_status": "Historical diagnosis is not current verification",
            }
        guidance = json.dumps(data, ensure_ascii=False)
        diagnosis = data.get("diagnosis")
        if isinstance(diagnosis, dict) and not data.get("tiny_patch_proposed"):
            diagnosis = cast("dict[str, object]", diagnosis)
            patch = diagnosis.get("patch")
            if isinstance(patch, dict):
                patch = cast("dict[str, object]", patch)
                diagnosis["patch"] = {
                    "path": patch["path"],
                    "sha256": patch["sha256"],
                    "executable_patch_omitted": "Use next_steps; full proposal is in the ledger",
                }
                guidance = json.dumps(data, ensure_ascii=False)
        # Prior source excerpts are optional, exact audit evidence. Drop whole
        # excerpts before allowing new advice to crowd out the current state.
        items = list(context.items)
        while sum(len(i.content) for i in items) + len(guidance) > 32000:
            optional = next((i for i in items if ":source-event:" in str(i.item_id)), None)
            if optional is None:
                break  # Mandatory context still passes the provider's fail-closed admission.
            items.remove(optional)
        item = ContextItem(
            item_id=ContextItemId(f"{context.run_id}:repair-specialist"),
            content=guidance,
            trust=TrustClass.MODEL_INFERENCE,
            source=ContextSource(
                origin=TrustClass.MODEL_INFERENCE,
                reference="repair-ledger:latest-settled",
                detail="Diagnosis is evidence, never authority; check its source hashes",
            ),
            created_at=context.assembled_at,
        )
        return replace(context, items=(*items, item))


class EscalatingRepairModel:
    def __init__(  # noqa: PLR0913 - frozen composition dependencies
        self,
        model: ModelPort,
        *,
        store: StateStorePort,
        journal: RepairJournal,
        root: Path,
        task: CodingTaskConfig,
        config: CampaignConfig,
        identity: Callable[[], str],
        writable: Callable[[str], bool],
        specialist: GeminiRepair | None = None,
        prior_runs: tuple[RunId, ...] = (),
        parent_budget: PaidBudgetPort | None = None,
    ) -> None:
        self.model, self.store, self.journal = model, store, journal
        self.root, self.task, self.config = root, task, config
        self.identity, self.writable, self.specialist = identity, writable, specialist
        self.parent_budget = parent_budget
        self.prior_runs = prior_runs

    @property
    def capabilities(self) -> ModelCapabilities:
        return self.model.capabilities

    def propose_action(self, context: ModelContext) -> ModelTurn:
        events = self.store.events_for(context.run_id)
        samples = failure_samples(events)
        recovery = self._recovery_events()
        reason = (
            "repeated_verification"
            if escalation_due(samples)
            else "stagnation"
            if stall_escalation_due(stagnant_turns(events))
            else "recorded_stall_recovery"
            if recovery
            else ""
        )
        if not reason:
            return self.model.propose_action(context)
        current = replay(context.run_id, events)
        diagnostic = current.last_verification
        if diagnostic is None and recovery:
            diagnostic = replay(recovery[0].run_id, recovery).last_verification
        key = failure_identity(diagnostic or "No new source or patch evidence")
        with self.journal.connect() as db:
            account = self.journal.load(db)
        if key in account.keys or len(account.keys) >= 3:
            return self.model.propose_action(context)
        state = replay(context.run_id, events)
        if (
            state.started_at is None
            or (context.assembled_at - state.started_at).total_seconds() + 180
            >= self.config.max_seconds
        ):
            return self.model.propose_action(context)
        if reason != "repeated_verification" and not any(
            isinstance(e, ActionProposed)
            and str(e.proposal.action_id).startswith("repair-reproduce-")
            for e in events
        ):
            # Reproduce with an existing immutable command; its usual metadata,
            # timeout, sandbox and outcome journal remain authoritative.
            preferred = next(
                (c.name for c in self.task.checks if c.name == "check_project"),
                self.task.checks[0].name,
            )
            reproduce = next(
                (
                    c.name
                    for c in self.task.checks
                    if f"command:{c.name}: failed" in (diagnostic or "")
                ),
                preferred,
            )
            return ModelTurn(
                action=ActionProposal(
                    ActionId(f"repair-reproduce-{context.run_id}"), reproduce, {}
                ),
                usage=UsageDelta(),
            )
        if self.specialist is not None:
            return self._diagnose(context, key, self.specialist)
        # A separate connection pool and stateless role. No orchestrator prompt,
        # chat history, client or credential material is handed to the local model.
        with httpx.Client(timeout=httpx.Timeout(180), trust_env=False) as client:
            specialist = GeminiRepair(load_gemini_key(Path(".loopforge/secrets.env")), client)
            return self._diagnose(context, key, specialist)

    def _recovery_events(self) -> tuple[Event, ...]:
        references = self.prior_runs
        if self.config.repair_recovery_run_id is not None:
            references = (*references, RunId(self.config.repair_recovery_run_id))
        for run_id in reversed(references):
            events = self.store.events_for(run_id)
            if not events or not any(
                isinstance(e, ModelAttemptFailed) and e.reason_code == "CODING_NO_NEW_EVIDENCE"
                for e in events
            ):
                continue
            if replay(run_id, events).status is not RunStatus.FAILED or stagnant_turns(events) < 8:
                continue
            packet = next((e for e in reversed(events) if isinstance(e, ContextAssembled)), None)
            if packet is not None and current_patch(packet) == self.identity():
                return events
        return ()

    def _packet(self, context: ModelContext) -> str:
        events = self.store.events_for(context.run_id)
        state = replay(context.run_id, events)
        recovery = self._recovery_events()
        diagnostic = (
            state.last_verification
            or (replay(recovery[0].run_id, recovery).last_verification if recovery else "")
            or ""
        )
        proposals = [e.proposal for e in (*recovery, *events) if isinstance(e, ActionProposed)]
        check_actions = {
            p.action_id for p in proposals if p.tool_name in {c.name for c in self.task.checks}
        }
        check_output = next(
            (
                e.error_message
                for e in reversed(events)
                if isinstance(e, ToolFailed) and e.action_id in check_actions
            ),
            "",
        )
        names = [p for p in self.task.required_changed_paths if p in diagnostic + check_output]
        names += [p.arguments.get("path", "") for p in reversed(proposals[-12:])]
        names += self.task.required_changed_paths
        sources: list[dict[str, str]] = []
        used: set[str] = set()
        for name in names:
            if name in used or not source_allowed(name) or not self.writable(name):
                continue
            used.add(name)
            path = self.root / name
            if (
                not path.is_file()
                or path.is_symlink()
                or not path.resolve().is_relative_to(self.root.resolve())
                or any(p.is_symlink() for p in path.parents if p != self.root.parent)
            ):
                continue
            raw = path.read_bytes()
            if len(raw) > 16000:
                continue
            source = {
                "path": name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "source": raw.decode("utf-8"),
            }
            if len(json.dumps([*sources, source], ensure_ascii=False).encode()) <= 23000:
                sources.append(source)
        packet = {
            "objective": self.task.objective[:4000],
            "basis_patch_sha256": self.identity(),
            "diagnostics_untrusted": diagnostic[:8000],
            "diagnostics_omitted_characters": max(0, len(diagnostic) - 8000),
            "reproduced_check_output_untrusted": diagnostic_excerpt(check_output),
            "escalation_evidence": {
                "stagnant_turns": stagnant_turns(events),
                "recorded_stall_run_id": str(recovery[0].run_id) if recovery else None,
            },
            "allowed_edit_paths": self.task.allowed_paths,
            "required_checks": [c.name for c in self.task.execution_checks],
            "attempts_untrusted": [
                {"tool": p.tool_name, "arguments": {k: v[:1200] for k, v in p.arguments.items()}}
                for p in proposals[-6:]
            ],
            "source_untrusted": sources,
            "omitted_source_paths": sorted(
                set(self.task.required_changed_paths) - {s["path"] for s in sources}
            ),
            "note": "Omitted source was not inspected. Patch only exact supplied source.",
        }
        # Omit whole optional source files rather than sending an oversized packet
        # that silently prevents the already-authorized escalation from happening.
        body = json.dumps(packet, ensure_ascii=False)
        while len(body.encode()) > 40000 and sources:
            sources.pop()
            packet["omitted_source_paths"] = sorted(
                set(self.task.required_changed_paths) - {s["path"] for s in sources}
            )
            body = json.dumps(packet, ensure_ascii=False)
        return body

    def _diagnose(self, context: ModelContext, key: str, specialist: GeminiRepair) -> ModelTurn:
        basis = self.identity()
        local_reserved = False
        try:
            packet = self._packet(context)
            request = specialist.prepare(packet)
            state = replay(context.run_id, self.store.events_for(context.run_id))
            if state.cost_usd + request.maximum_microusd / 1_000_000 > 1:
                return self.model.propose_action(context)
            token_bound = json.loads(request.trace)["input_bound"] + 8192
            if state.total_tokens + token_bound > self.config.max_tokens:
                return self.model.propose_action(context)
            self.journal.append(
                RepairReserved(key, request.maximum_microusd),
                json.dumps(
                    {
                        "run_id": str(context.run_id),
                        "request": json.loads(request.trace),
                        "packet": json.loads(packet),
                    }
                ),
            )
            local_reserved = True
            if self.parent_budget:
                self.parent_budget.reserve("repair:" + key, "repair", request.maximum_microusd)
        except (SupervisorFailure, ValueError):
            if local_reserved:
                self.journal.append(
                    RepairSettled(key, 0),
                    json.dumps({"failure": "parent reservation denied; no provider call"}),
                )
            return self.model.propose_action(context)
        charged = request.maximum_microusd
        usage = UsageDelta(
            cost_usd=charged / 1_000_000,
            input_tokens=json.loads(request.trace)["input_bound"],
            output_tokens=8192,
        )
        action = ActionProposal(
            ActionId(
                f"repair-guidance-{context.run_id}-{len(self.store.events_for(context.run_id))}"
            ),
            FEEDBACK_TOOL,
            {},
        )
        try:
            diagnosis, charged, trace, usage = specialist.diagnose(request)
            patch = diagnosis.patch
            apply = bool(
                patch
                and tiny_patch(diagnosis)
                and self.writable(patch.path)
                and self.identity() == basis
                and any(
                    s["path"] == patch.path and s["sha256"] == patch.sha256
                    for s in json.loads(packet)["source_untrusted"]
                )
            )
            if patch is not None and apply:
                path = self.root / patch.path
                apply = (
                    path.is_file()
                    and not path.is_symlink()
                    and path.resolve().is_relative_to(self.root.resolve())
                    and not any(p.is_symlink() for p in path.parents if p != self.root.parent)
                    and hashlib.sha256(path.read_bytes()).hexdigest() == patch.sha256
                )
                if apply:
                    current = path.read_text()
                    apply = current.count(patch.old) == 1 and small_line_change(
                        current, current.replace(patch.old, patch.new, 1)
                    )
            advice = {
                "basis_patch_sha256": basis,
                "diagnosis": diagnosis.model_dump(mode="json"),
                "tiny_patch_proposed": apply,
                "verification_status": "unverified; fixed checks and fresh review required",
            }
            if self.parent_budget:
                self.parent_budget.settle("repair:" + key, charged)
            self.journal.append(RepairSettled(key, charged), json.dumps(advice, ensure_ascii=False))
            # Provider trace stays separate from the small state packet.
            trace_path = self.journal.path.parent / f"repair-provider-{key}.json"
            trace_path.write_text(trace)
            if patch is not None and apply:
                action = ActionProposal(
                    action.action_id,
                    "apply_edits",
                    {
                        "path": patch.path,
                        "sha256": patch.sha256,
                        "edits": json.dumps([{"old": patch.old, "new": patch.new}]),
                    },
                )
        except SupervisorFailure as exc:
            if self.parent_budget and exc.charged_microusd is not None:
                self.parent_budget.settle("repair:" + key, exc.charged_microusd)
            charged = (
                request.maximum_microusd if exc.charged_microusd is None else exc.charged_microusd
            )
            self.journal.append(
                RepairSettled(key, charged),
                json.dumps(
                    {"basis_patch_sha256": basis, "failure": str(exc), "automatic_retry": False}
                ),
            )
        return ModelTurn(action=action, usage=replace(usage, cost_usd=charged / 1_000_000))
