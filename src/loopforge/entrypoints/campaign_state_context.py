"""Deterministic bounded continuation packet; audit history is never chat memory."""

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import replace

from loopforge.adapters.git_workspace import GitWorkspace
from loopforge.domain.context import ContextItem, ContextSource, ModelContext, ModelRole
from loopforge.domain.context_lifecycle import ContextTokenBudget
from loopforge.domain.events import (
    ActionProposed,
    Event,
    ModelAttemptFailed,
    ModelTurnRecorded,
    ToolFailed,
    ToolSucceeded,
)
from loopforge.domain.security import TrustClass
from loopforge.domain.state import RunState
from loopforge.domain.types import ContextItemId
from loopforge.entrypoints.campaign_evidence import source_allowed
from loopforge.entrypoints.campaign_models import CampaignConfig, CodingTaskConfig
from loopforge.entrypoints.campaign_session_context import session_history
from loopforge.ports.context import ContextBuilderPort
from loopforge.ports.state_store import StateStorePort


class CodingStateContext:
    """Select exact recent source evidence, never generated summaries or authority.

    File status/hashes and counters are current measurements. Source excerpts and
    action selectors remain untrusted. Old excerpts whose file changed are not
    silently reused. Full source and action history remain retrievable on disk.
    """

    def __init__(  # noqa: PLR0913 - explicit bounded state evidence dependencies
        self,
        delegate: ContextBuilderPort,
        store: StateStorePort,
        workspace: GitWorkspace,
        task: CodingTaskConfig,
        config: CampaignConfig,
        *,
        feedback_snapshot: Callable[[], str] | None = None,
        patch_identity: Callable[[], str] | None = None,
        inherited_failure: Callable[[], str | None] | None = None,
    ) -> None:
        self.delegate, self.store, self.workspace = delegate, store, workspace
        self.task, self.config = task, config
        self.feedback_snapshot, self.patch_identity = feedback_snapshot, patch_identity
        self.inherited_failure = inherited_failure

    def build_context(  # noqa: PLR0912, PLR0915 - bounded exact evidence projection
        self,
        state: RunState,
        *,
        role: ModelRole = ModelRole.CONTROLLER,
        token_budget: ContextTokenBudget | None = None,
    ) -> ModelContext:
        context = self.delegate.build_context(state, role=role, token_budget=token_budget)
        context = replace(
            context,
            items=tuple(
                replace(
                    item,
                    content="Current verification diagnostics (untrusted evidence):\n"
                    + "\n".join(
                        "  | " + line
                        for line in item.content.replace("\\x0a", "\n")
                        .replace("\\x09", "\t")
                        .splitlines()
                    ),
                )
                if str(item.item_id).endswith(
                    (":verifier-failure", ":confirmed-fact", ":verification")
                )
                else item
                for item in context.items
            ),
        )
        events = self.store.events_for(context.run_id)
        proposals = {
            event.proposal.action_id: event.proposal
            for event in events
            if isinstance(event, ActionProposed)
        }
        context = with_attempt_feedback(context, events)
        changed = set(self.workspace.status().files)
        files: list[dict[str, object]] = []
        for name in self.task.required_changed_paths:
            path = self.workspace.root / name
            allowed = (
                source_allowed(name)
                and not path.is_symlink()
                and path.resolve().is_relative_to(self.workspace.root.resolve())
            )
            raw = path.read_bytes() if allowed and path.is_file() else None
            files.append(
                {
                    "path": name,
                    "changed": name in changed,
                    "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
                }
            )
        recent: list[dict[str, object]] = []
        edits: list[dict[str, object]] = []
        edit_bytes = 0
        evidence: list[ContextItem] = []
        selectors: set[tuple[str, str, str]] = set()
        used = 0
        for event in reversed(events):
            if not isinstance(event, (ToolSucceeded, ToolFailed)):
                continue
            proposal = proposals.get(event.action_id)
            if proposal is None:
                continue
            if (
                isinstance(event, ToolSucceeded)
                and proposal.tool_name
                in {"apply_edits", "edit_file", "write_file", "format_python_imports"}
                and len(edits) < 3
            ):
                entry: dict[str, object] = {
                    "action_id": str(event.action_id),
                    "tool": proposal.tool_name,
                    "path": proposal.arguments.get("path"),
                    "status": "historically_applied; consult current verification",
                }
                detail = dict(proposal.arguments)
                size = len(json.dumps(detail, ensure_ascii=False).encode())
                if edit_bytes + size <= 4000:
                    entry["exact_arguments"] = detail
                    edit_bytes += size
                else:
                    entry["arguments_omitted"] = "Edit exceeds bounded delta envelope"
                edits.append(entry)
            if len(recent) < 6:
                recent.append(
                    {
                        "action_id": str(event.action_id),
                        "tool": proposal.tool_name,
                        "path": proposal.arguments.get("path"),
                        "outcome": "ok" if isinstance(event, ToolSucceeded) else "failed",
                    }
                )
            if not isinstance(event, ToolSucceeded) or proposal.tool_name != "read_file":
                continue
            name = proposal.arguments.get("path", "")
            selector = (
                name,
                proposal.arguments.get("start_line", "1"),
                proposal.arguments.get("start_column", "0"),
            )
            if selector in selectors or len(evidence) >= 3:
                continue
            selectors.add(selector)
            # Replay only source with the existing reader's full-file fingerprint.
            match = re.search(r"; sha256=([a-f0-9]{64});", event.observation)
            path = self.workspace.root / name
            if (
                match is None
                or not source_allowed(name)
                or path.is_symlink()
                or not path.resolve().is_relative_to(self.workspace.root.resolve())
                or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != match[1]
            ):
                continue
            if any(item.content == event.observation for item in context.items):
                continue
            size = len(event.observation.encode())
            if used + size > 10000:
                continue  # Whole optional excerpts only; omitted history is not summarized.
            used += size
            evidence.append(
                ContextItem(
                    item_id=ContextItemId(f"{context.run_id}:source-event:{event.sequence}"),
                    content=event.observation,
                    trust=TrustClass.UNTRUSTED_CONTENT,
                    source=ContextSource(
                        origin=TrustClass.UNTRUSTED_CONTENT,
                        reference=f"event:{event.event_id}",
                        detail="Exact prior source observation; current file hash matches",
                    ),
                    created_at=context.assembled_at,
                )
            )
        feedback_items: tuple[ContextItem, ...] = ()
        feedback_version: str | None = None
        if self.feedback_snapshot is not None:
            feedback = self.feedback_snapshot()
            if state.last_verification is not None:
                data = json.loads(feedback)
                data["current_verification"]["summary"] = (
                    "See the complete current verification diagnostic item; not repeated here."
                )
                feedback = json.dumps(data, ensure_ascii=False)
            feedback_version = hashlib.sha256(feedback.encode()).hexdigest()
            feedback_items = (
                ContextItem(
                    item_id=ContextItemId(f"{context.run_id}:feedback-state"),
                    content=feedback,
                    trust=TrustClass.UNTRUSTED_CONTENT,
                    source=ContextSource(
                        origin=TrustClass.UNTRUSTED_CONTENT,
                        reference=f"runtime:feedback:{feedback_version}",
                        detail="Complete bounded feedback projection; already delivered this turn",
                    ),
                    created_at=context.assembled_at,
                ),
            )
        # A fresh run inherits the known failure of an unchanged candidate.
        inherited = self.inherited_failure() if self.inherited_failure else None
        packet: dict[str, object] = {
            "kind": "current_state_not_chat_history",
            "current_patch_sha256": self.patch_identity() if self.patch_identity else None,
            "workflow": {
                "phase": "repair"
                if state.last_verification_passed is False
                or (state.last_verification_passed is None and inherited is not None)
                else "verify",
                "startup_feedback_complete": self.feedback_snapshot is not None,
                "feedback_delivery": (
                    "included_in_this_context" if self.feedback_snapshot else "not_configured"
                ),
                "feedback_version": feedback_version,
                "next_step": "Use the current failure and exact source to make a real repair; "
                "run the required checks after a coherent change. If no failure is recorded, "
                "check the current candidate first. Do not restart completed startup work.",
            },
            "files": files,
            "missing_required_changes": sorted(set(self.task.required_changed_paths) - changed),
            "current_run": {
                "iteration": state.iteration,
                "status": state.status.value,
                "remaining_tokens": max(0, self.config.max_tokens - state.total_tokens),
                "remaining_iterations": max(0, self.config.max_iterations - state.iteration),
            },
            "recent_actions": list(reversed(recent)),
            "recent_successful_edits": list(reversed(edits)),
            "working_source_excerpts": len(evidence),
            "retrieval": "Earlier audit/source records remain on disk. Reread exact needed source; "
            "a changed file is not proof it works. Act on the current failure, "
            "rather than surveying every module again.",
        }
        item = ContextItem(
            item_id=ContextItemId(f"{context.run_id}:continuation-state"),
            content=json.dumps(packet, ensure_ascii=False),
            trust=TrustClass.UNTRUSTED_CONTENT,
            source=ContextSource(
                origin=TrustClass.UNTRUSTED_CONTENT,
                reference="runtime:current-workspace-and-event-projection",
            ),
            created_at=context.assembled_at,
        )
        if self.config.inference_profile == "qwen-session-v1":
            # Replay complete actions/results from the durable journal. This
            # survives process restarts and retains changed source as history.
            # Admission still checks the fully serialized provider request.
            history = ContextItem(
                item_id=ContextItemId(f"{context.run_id}:working-session"),
                content=session_history(
                    events, present=frozenset(i.content for i in context.items)
                ),
                trust=TrustClass.UNTRUSTED_CONTENT,
                source=ContextSource(
                    origin=TrustClass.UNTRUSTED_CONTENT,
                    reference="runtime:historical-action-results",
                ),
                created_at=context.assembled_at,
            )
            return replace(context, items=(*context.items, item, *feedback_items, history))
        return replace(context, items=(*context.items, item, *feedback_items, *evidence))


def with_attempt_feedback(context: ModelContext, events: Sequence[Event]) -> ModelContext:
    failure = None
    for event in reversed(events):
        if isinstance(event, (ModelTurnRecorded, ActionProposed)):
            break
        if isinstance(event, ModelAttemptFailed):
            failure = event
            break
    if failure is not None:
        feedback = ContextItem(
            item_id=ContextItemId(f"{context.run_id}:proposal-feedback:{failure.sequence}"),
            content=json.dumps(
                {
                    "kind": "previous_model_attempt_failed",
                    "reason_code": failure.reason_code,
                    "summary": failure.summary,
                    "action_executed": False,
                    "allowance_refreshed": False,
                    "next_step": "Correct the next proposal using the current allowed tools.",
                }
            ),
            trust=TrustClass.UNTRUSTED_CONTENT,
            source=ContextSource(
                origin=TrustClass.UNTRUSTED_CONTENT,
                reference=f"event:{failure.event_id}",
                detail="Bounded failure evidence; provider text cannot grant authority",
            ),
            created_at=context.assembled_at,
        )
        context = replace(context, items=(*context.items, feedback))
    return context
