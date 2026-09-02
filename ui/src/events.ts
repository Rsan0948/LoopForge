// Typed mirror of the wire shape produced by JsonEventCodec
// (src/loopforge/adapters/json_events.py). Both the REST events endpoint and
// the WebSocket stream deliver the same envelope:
//   {schema_version, event_type, event: {...payload}}

export interface EventBase {
  event_id: string;
  run_id: string;
  occurred_at: string; // ISO 8601, timezone-aware
  sequence: number;
  caused_by: string | null;
}

export interface ActionProposal {
  action_id: string;
  tool_name: string;
  arguments: Record<string, string>;
  expected_observation: string | null;
}

export interface ToolMetadata {
  name: string;
  risk: string;
  required_permission: string;
  side_effect: string;
  retry: string;
  idempotency: string;
  approval: string;
  timeout_seconds: number;
  sensitivity: string;
}

export interface UsageDelta {
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
}

export interface ContextItemSnapshot {
  item_id: string;
  content: string;
  trust: string;
  source: { origin: string; reference: string; detail: string };
  sensitivity: string;
  created_at: string;
  supersedes: string | null;
  expires_at: string | null;
}

// -- Per-type payloads (domain/events.py, lowered by the codec) ---------------

export interface RunStartedPayload extends EventBase {
  objective: string;
  parent_run_id: string | null;
}
export interface PlanCreatedPayload extends EventBase {
  plan: string;
}
export interface ActionProposedPayload extends EventBase {
  proposal: ActionProposal;
}
export interface ActionAuthorizedPayload extends EventBase {
  proposal: ActionProposal;
  tool_metadata: ToolMetadata;
}
export interface ActionRejectedPayload extends EventBase {
  proposal: ActionProposal;
  reason_code: string;
}
export interface ToolExecutionStartedPayload extends EventBase {
  action_id: string;
  attempt: number;
  idempotency_key: string | null;
}
export interface ToolSucceededPayload extends EventBase {
  action_id: string;
  observation: string;
  attempt: number;
}
export interface ToolFailedPayload extends EventBase {
  action_id: string;
  error_code: string;
  error_message: string;
  failure_class: string;
  attempt: number;
}
export interface RetryScheduledPayload extends EventBase {
  action_id: string;
  next_attempt: number;
  delay_seconds: number;
  reason_code: string;
}
export interface CircuitOpenedPayload extends EventBase {
  tool_name: string;
  reason_code: string;
}
export interface VerificationPassedPayload extends EventBase {
  summary: string;
}
export interface VerificationFailedPayload extends EventBase {
  summary: string;
  score: number | null;
}
export interface ReflectionRecordedPayload extends EventBase {
  reflection: string;
}
export interface ContextAssembledPayload extends EventBase {
  context_items: ContextItemSnapshot[];
  prompt_template_id: string | null;
  prompt_template_version: string | null;
}
export interface ArtifactRecordedPayload extends EventBase {
  kind: string;
  label: string;
  content: string;
}
export interface BudgetDebitedPayload extends EventBase {
  usage: UsageDelta;
}
export interface ModelTurnRecordedPayload extends EventBase {
  provider: string;
  model: string;
  action_id: string;
}
export interface ApprovalRequestedPayload extends EventBase {
  action_id: string;
  reason: string;
}
export interface ApprovalGrantedPayload extends EventBase {
  action_id: string;
}
export interface ApprovalRejectedPayload extends EventBase {
  action_id: string;
  reason: string;
}
export interface OperatorInstructionPayload extends EventBase {
  instruction: string;
  amends_objective: boolean;
}
export interface RunStoppedPayload extends EventBase {
  reason: string;
  summary: string;
}
export interface WorkerSpawnedPayload extends EventBase {
  worker_id: string;
  worker_run_id: string;
  workspace_id: string;
  objective: string;
  budget_share_cost_usd: number;
}
export interface WorkerStoppedPayload extends EventBase {
  worker_id: string;
  outcome: string;
  summary: string;
}
export interface WorkerMergedPayload extends EventBase {
  worker_id: string;
  outcome: string;
  revision: string | null;
  detail: string;
}

export type EventPayload =
  | RunStartedPayload
  | PlanCreatedPayload
  | ActionProposedPayload
  | ActionAuthorizedPayload
  | ActionRejectedPayload
  | ToolExecutionStartedPayload
  | ToolSucceededPayload
  | ToolFailedPayload
  | RetryScheduledPayload
  | CircuitOpenedPayload
  | VerificationPassedPayload
  | VerificationFailedPayload
  | ReflectionRecordedPayload
  | ContextAssembledPayload
  | ArtifactRecordedPayload
  | BudgetDebitedPayload
  | ModelTurnRecordedPayload
  | ApprovalRequestedPayload
  | ApprovalGrantedPayload
  | ApprovalRejectedPayload
  | OperatorInstructionPayload
  | RunStoppedPayload
  | WorkerSpawnedPayload
  | WorkerStoppedPayload
  | WorkerMergedPayload;

export interface EventEnvelope {
  schema_version: number;
  event_type: string;
  event: EventPayload;
}

function truncate(text: string, max: number): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}

/** Human-readable label + one-line summary for an event-stream row. */
export function describeEvent(envelope: EventEnvelope): { label: string; summary: string } {
  const e = envelope.event;
  switch (envelope.event_type) {
    case "RunStarted":
      return { label: "Run started", summary: truncate((e as RunStartedPayload).objective, 120) };
    case "PlanCreated":
      return { label: "Plan created", summary: truncate((e as PlanCreatedPayload).plan, 120) };
    case "ActionProposed": {
      const p = (e as ActionProposedPayload).proposal;
      return { label: "Action proposed", summary: `${p.tool_name} (${shortId(p.action_id)})` };
    }
    case "ActionAuthorized": {
      const p = (e as ActionAuthorizedPayload).proposal;
      return { label: "Action authorized", summary: p.tool_name };
    }
    case "ActionRejected": {
      const p = e as ActionRejectedPayload;
      return {
        label: "Action rejected",
        summary: `${p.proposal.tool_name}: ${p.reason_code}`,
      };
    }
    case "ToolExecutionStarted": {
      const p = e as ToolExecutionStartedPayload;
      return { label: "Tool execution started", summary: `attempt ${p.attempt}` };
    }
    case "ToolSucceeded": {
      const p = e as ToolSucceededPayload;
      return { label: "Tool succeeded", summary: truncate(p.observation, 120) };
    }
    case "ToolFailed": {
      const p = e as ToolFailedPayload;
      return {
        label: "Tool failed",
        summary: `${p.error_code} (${p.failure_class}): ${truncate(p.error_message, 100)}`,
      };
    }
    case "RetryScheduled": {
      const p = e as RetryScheduledPayload;
      return {
        label: "Retry scheduled",
        summary: `attempt ${p.next_attempt} in ${p.delay_seconds}s (${p.reason_code})`,
      };
    }
    case "CircuitOpened": {
      const p = e as CircuitOpenedPayload;
      return { label: "Circuit opened", summary: `${p.tool_name}: ${p.reason_code}` };
    }
    case "VerificationPassed":
      return { label: "Verification passed", summary: truncate((e as VerificationPassedPayload).summary, 120) };
    case "VerificationFailed": {
      const p = e as VerificationFailedPayload;
      const score = p.score === null ? "" : ` score=${p.score.toFixed(2)}`;
      return { label: "Verification failed", summary: `${truncate(p.summary, 110)}${score}` };
    }
    case "ReflectionRecorded":
      return { label: "Reflection recorded", summary: truncate((e as ReflectionRecordedPayload).reflection, 120) };
    case "ContextAssembled": {
      const p = e as ContextAssembledPayload;
      return { label: "Context assembled", summary: `${p.context_items.length} item(s)` };
    }
    case "ArtifactRecorded": {
      const p = e as ArtifactRecordedPayload;
      return { label: "Artifact recorded", summary: `${p.kind}: ${p.label}` };
    }
    case "BudgetDebited": {
      const p = e as BudgetDebitedPayload;
      return {
        label: "Budget debited",
        summary: `$${p.usage.cost_usd.toFixed(4)} · ${p.usage.input_tokens} in / ${p.usage.output_tokens} out`,
      };
    }
    case "ModelTurnRecorded": {
      const p = e as ModelTurnRecordedPayload;
      return { label: "Model turn", summary: `${p.provider}/${p.model}` };
    }
    case "ApprovalRequested": {
      const p = e as ApprovalRequestedPayload;
      return { label: "Approval requested", summary: truncate(p.reason, 120) };
    }
    case "ApprovalGranted":
      return { label: "Approval granted", summary: shortId((e as ApprovalGrantedPayload).action_id) };
    case "ApprovalRejected": {
      const p = e as ApprovalRejectedPayload;
      return { label: "Approval rejected", summary: truncate(p.reason, 120) };
    }
    case "OperatorInstruction": {
      const p = e as OperatorInstructionPayload;
      const amend = p.amends_objective ? " (amends objective)" : "";
      return { label: "Operator instruction", summary: `${truncate(p.instruction, 110)}${amend}` };
    }
    case "RunStopped": {
      const p = e as RunStoppedPayload;
      return { label: "Run stopped", summary: `${p.reason}: ${truncate(p.summary, 100)}` };
    }
    case "WorkerSpawned": {
      const p = e as WorkerSpawnedPayload;
      return { label: "Worker spawned", summary: `${p.worker_id} ($${p.budget_share_cost_usd.toFixed(2)})` };
    }
    case "WorkerStopped": {
      const p = e as WorkerStoppedPayload;
      return { label: "Worker stopped", summary: `${p.worker_id}: ${p.outcome}` };
    }
    case "WorkerMerged": {
      const p = e as WorkerMergedPayload;
      const rev = p.revision === null ? "" : ` @ ${p.revision.slice(0, 10)}`;
      return { label: "Worker merged", summary: `${p.worker_id}: ${p.outcome}${rev}` };
    }
    default:
      return { label: envelope.event_type, summary: "" };
  }
}
