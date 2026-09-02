// Typed client for the loopforge operator server
// (src/loopforge/entrypoints/server.py). The GUI is a projection + command
// issuer only: every mutation is a POST here; server responses are
// authoritative. All URLs are relative so the SPA works under the backend
// mount with no configuration.

import type { EventEnvelope } from "./events";

// -- RunStatus / StopReason mirror domain/types.py ----------------------------

export type RunStatus =
  | "created"
  | "planning"
  | "ready"
  | "acting"
  | "verifying"
  | "reflecting"
  | "waiting_for_approval"
  | "succeeded"
  | "failed"
  | "stalled"
  | "budget_exhausted"
  | "cancelled";

export const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set([
  "succeeded",
  "failed",
  "stalled",
  "budget_exhausted",
  "cancelled",
]);

export interface SessionEntry {
  run_id: string;
  objective: string;
  status: RunStatus;
  started_at: string;
  last_occurred_at: string;
  cost_usd: number;
  stop_reason: string | null;
  driving: boolean;
  managed: boolean;
}

export interface BudgetInfo {
  max_cost_usd: number;
  max_iterations: number;
  max_total_tokens: number | null;
  max_elapsed_seconds: number | null;
}

export interface PendingApproval {
  action_id: string;
  reason: string;
}

export interface CurrentProposal {
  action_id: string;
  tool_name: string;
  arguments: Record<string, string>;
}

export interface SessionDetail extends SessionEntry {
  budget: BudgetInfo | null;
  iteration: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  last_verification: string | null;
  last_verification_passed: boolean | null;
  last_verification_score: number | null;
  plan: string | null;
  current_proposal: CurrentProposal | null;
  waiting_for_approval: boolean;
  pending_approval: PendingApproval | null;
  last_approval_rejection: string | null;
  operator_instructions: string[];
  version: number;
  repository: string | null;
  driver_error?: string;
}

export interface ProfileInfo {
  name: string;
  path: string;
}

export interface ArtifactInfo {
  sequence: number;
  kind: string;
  label: string;
  content: string;
  occurred_at: string;
}

export interface EventsPage {
  events: EventEnvelope[];
  latest_sequence: number;
}

// -- Inline profile request (mirrors InlineProfileRequest) --------------------

export interface InlineCheck {
  name: string;
  kind: string;
  argv: string[];
  timeout_seconds: number;
  cpu_seconds: number | null;
}

export interface InlineProfile {
  repository: string;
  objective: string;
  task_id?: string;
  checks: InlineCheck[];
  acceptance: {
    required: string[];
    allowed_prefixes: string[];
    require_change: boolean;
    max_changed_files: number | null;
  };
  sandbox: {
    container_image: string | null;
    environment: Record<string, string>;
    max_memory_bytes: number | null;
    local_python: string | null;
  };
  model: {
    provider: string;
    name: string | null;
    tier: string;
  };
  budget: {
    max_cost_usd: number;
    max_iterations: number;
    max_total_tokens: number | null;
    max_elapsed_seconds: number | null;
  };
  /** Operator approval gate (PACS-014): named tools pause for durable approval. */
  approval: { required_for: string[] } | null;
}

// -- Errors --------------------------------------------------------------------

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`HTTP ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: init?.body !== undefined ? { "Content-Type": "application/json" } : undefined,
    ...init,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body: unknown = await response.json();
      if (typeof body === "object" && body !== null && "detail" in body) {
        const raw = (body as { detail: unknown }).detail;
        detail = typeof raw === "string" ? raw : JSON.stringify(raw);
      }
    } catch {
      // Keep the status text when the body is not JSON.
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

// -- REST endpoints --------------------------------------------------------------

export async function listSessions(): Promise<SessionEntry[]> {
  const data = await request<{ sessions: SessionEntry[] }>("/api/sessions");
  return data.sessions;
}

export async function listProfiles(): Promise<ProfileInfo[]> {
  const data = await request<{ profiles: ProfileInfo[] }>("/api/profiles");
  return data.profiles;
}

export async function createSessionByProfile(profilePath: string): Promise<string> {
  const data = await post<{ run_id: string }>("/api/sessions", { profile_path: profilePath });
  return data.run_id;
}

export async function createSessionInline(inline: InlineProfile): Promise<string> {
  const data = await post<{ run_id: string }>("/api/sessions", { inline });
  return data.run_id;
}

export function getSession(runId: string): Promise<SessionDetail> {
  return request<SessionDetail>(`/api/sessions/${encodeURIComponent(runId)}`);
}

export function getEvents(runId: string, afterSequence = 0, limit = 500): Promise<EventsPage> {
  return request<EventsPage>(
    `/api/sessions/${encodeURIComponent(runId)}/events?after_sequence=${afterSequence}&limit=${limit}`,
  );
}

export async function getArtifacts(runId: string): Promise<ArtifactInfo[]> {
  const data = await request<{ artifacts: ArtifactInfo[] }>(
    `/api/sessions/${encodeURIComponent(runId)}/artifacts`,
  );
  return data.artifacts;
}

export interface StatusResponse {
  run_id: string;
  status: RunStatus;
}

export function startSession(runId: string): Promise<StatusResponse> {
  return post<StatusResponse>(`/api/sessions/${encodeURIComponent(runId)}/start`);
}

export function pauseSession(runId: string): Promise<StatusResponse> {
  return post<StatusResponse>(`/api/sessions/${encodeURIComponent(runId)}/pause`);
}

export function resumeSession(runId: string): Promise<StatusResponse> {
  return post<StatusResponse>(`/api/sessions/${encodeURIComponent(runId)}/resume`);
}

export function stopSession(runId: string, summary?: string): Promise<StatusResponse> {
  return post<StatusResponse>(
    `/api/sessions/${encodeURIComponent(runId)}/stop`,
    summary === undefined ? undefined : { summary },
  );
}

export function approveAction(runId: string, actionId: string): Promise<StatusResponse> {
  return post<StatusResponse>(`/api/sessions/${encodeURIComponent(runId)}/approve`, {
    action_id: actionId,
  });
}

export function rejectAction(runId: string, actionId: string, reason: string): Promise<StatusResponse> {
  return post<StatusResponse>(`/api/sessions/${encodeURIComponent(runId)}/reject`, {
    action_id: actionId,
    reason,
  });
}

export function sendInstruction(
  runId: string,
  instruction: string,
  amendObjective = false,
): Promise<StatusResponse> {
  return post<StatusResponse>(`/api/sessions/${encodeURIComponent(runId)}/instructions`, {
    instruction,
    amend_objective: amendObjective,
  });
}

/** Selective rollback reverts `paths`; omitting `paths` sends an empty body,
 *  which the server treats as a full workspace reset to the base revision
 *  (see rollback_route in server.py). Denied with 409 while driving. */
export function rollbackRun(runId: string, paths?: string[]): Promise<StatusResponse> {
  return post<StatusResponse>(
    `/api/sessions/${encodeURIComponent(runId)}/rollback`,
    paths === undefined ? undefined : { paths },
  );
}

export function sessionWsUrl(runId: string): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}/ws/sessions/${encodeURIComponent(runId)}`;
}
