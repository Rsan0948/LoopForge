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
  last_verification_inconclusive: boolean;
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
    no_progress_limit: number | null;
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

/** Follow-up (PACS-014b): clone a TERMINAL run's wiring into a successor
 *  session whose objective carries the consolidated report. The successor
 *  stays quiescent unless `opts.start` is true — nothing auto-chains unless
 *  the operator explicitly asks. */
export function followUp(runId: string, opts?: { autoStart?: boolean }): Promise<{ run_id: string }> {
  return post<{ run_id: string }>(
    `/api/sessions/${encodeURIComponent(runId)}/follow-up`,
    opts === undefined ? undefined : { auto_start: opts.autoStart ?? false },
  );
}

// -- Provenance / explain / lineage (PACS-015) --------------------------------

export interface ProvenanceNode {
  node_id: string;
  kind: string;
  event_type: string;
  sequence: number;
  occurred_at: string;
  summary: string;
  attributes: Record<string, string>;
}

export interface ProvenanceEdge {
  source_id: string;
  target_id: string;
  kind: string;
}

export interface ProvenanceGraph {
  run_id: string;
  nodes: ProvenanceNode[];
  edges: ProvenanceEdge[];
}

export interface ProvenanceExplanation {
  run_id: string;
  node: ProvenanceNode;
  chain: ProvenanceNode[];
  supporting: ProvenanceNode[];
  outcomes: ProvenanceNode[];
}

export interface SessionLineage {
  run_id: string;
  parent_run_id: string | null;
  ancestors: string[];
  children: string[];
}

/** The derived provenance DAG: a pure on-demand projection of the event
 *  stream (one node per durable event). */
export function getProvenance(runId: string): Promise<ProvenanceGraph> {
  return request<ProvenanceGraph>(`/api/sessions/${encodeURIComponent(runId)}/provenance`);
}

/** The evidence chain answering "why did this node happen?". 404 when the
 *  node id is not in the run's graph. */
export function explainNode(runId: string, nodeId: string): Promise<ProvenanceExplanation> {
  return request<ProvenanceExplanation>(
    `/api/sessions/${encodeURIComponent(runId)}/explain?node=${encodeURIComponent(nodeId)}`,
  );
}

/** The run's follow-up lineage: parent, ancestors (parent-first), children. */
export function getLineage(runId: string): Promise<SessionLineage> {
  return request<SessionLineage>(`/api/sessions/${encodeURIComponent(runId)}/lineage`);
}

/** Force-release (PACS-015): the zombie escape hatch. Stops the run WITHOUT
 *  replay checks, so the literal `confirm: true` is only ever sent from the
 *  confirmation dialog — never defaulted into a bare call. */
export function forceRelease(
  runId: string,
  summary?: string,
): Promise<{ run_id: string; status: string }> {
  return post<{ run_id: string; status: string }>(
    `/api/sessions/${encodeURIComponent(runId)}/force-release`,
    { summary: summary ?? "force-released by operator", confirm: true },
  );
}

// -- Benchmark suite + eval reports (PACS-016) ----------------------------------
// Mirrors the M7 read-only routes in server.py: reports are operator-owned
// artifacts (written by `loopforge eval`), the console only ever reads them.

export interface BenchmarkTaskDescriptor {
  task_id: string;
  category: string;
  sandbox_mode: string;
  live_eligible: boolean;
  grader_ids: string[];
}

export interface BenchmarkSuiteInfo {
  version: string;
  /** M1 spec lock (suite_lock_hash). */
  lock_hash: string;
  /** M2 full content lock (benchmark_content_lock). */
  content_lock: string;
  tasks: BenchmarkTaskDescriptor[];
}

export interface EvalReportSummary {
  report_id: string;
  suite_version: string;
  lock_hash: string;
  config_ids: string[];
  task_ids: string[];
  created_at: string;
}

export interface ConfigReportRow {
  config_id: string;
  task_id: string;
  trials: number;
  successes: number;
  false_successes: number;
  success_rate: number;
  false_success_rate: number;
  mean_cost_usd: number;
  mean_latency_seconds: number;
  mean_total_tokens: number;
  mean_human_interventions: number;
  // Report schema v2 (PACS-017 M5): the extended Pareto axes. Always present
  // — v1 artifacts are zero-filled by the store's read shim.
  mean_context_tokens_used: number;
  mean_context_items_dropped: number;
  mean_recovery_events: number;
}

export interface EvalReport {
  report_id: string;
  suite_version: string;
  lock_hash: string;
  config_reports: ConfigReportRow[];
  pareto_config_ids: string[];
}

/** The LOCKED benchmark definition: version, both lock hashes, task specs. */
export function getBenchmarkSuite(): Promise<BenchmarkSuiteInfo> {
  return request<BenchmarkSuiteInfo>("/api/benchmark/suite");
}

/** Stored eval report summaries (EvalReportStore.list), empty when none. */
export async function listEvalReports(): Promise<EvalReportSummary[]> {
  const data = await request<{ reports: EvalReportSummary[] }>("/api/evals");
  return data.reports;
}

/** One full stored report. 404 (ApiError) when the report id is unknown. */
export function getEvalReport(reportId: string): Promise<EvalReport> {
  return request<EvalReport>(`/api/evals/${encodeURIComponent(reportId)}`);
}

// -- Candidate policy registry (PACS-017 M6) -----------------------------------

export type PolicyLifecycle = "candidate" | "shadowed" | "benchmarked" | "promoted" | "retired";

export interface PolicyRoutingInfo {
  default_tier: string;
  stall_escalation_threshold: number;
  budget_pressure_remaining_fraction: number | null;
}

export interface PolicyAllocationInfo {
  floor_tokens: number;
  ceiling_tokens: number;
  reserve_tokens: number;
  step_tokens: number;
  low_utilization_fraction: number;
}

export interface ExecutionPolicyInfo {
  policy_id: string;
  version: number;
  routing: PolicyRoutingInfo;
  context_allocation: PolicyAllocationInfo;
  verify_read_only_turns: boolean;
  worker_count: number | null;
}

/** One operator-owned registry record (policy + lifecycle + evidence basis). */
export interface PolicyRecordInfo {
  policy: ExecutionPolicyInfo;
  lifecycle: PolicyLifecycle;
  evidence_basis: string;
  note: string;
}

/** Every registered policy record (PolicyRegistryStore.list), empty when none. */
export async function listPolicies(): Promise<PolicyRecordInfo[]> {
  const data = await request<{ policies: PolicyRecordInfo[] }>("/api/policies");
  return data.policies;
}

/** The latest registered record for one policy id. 404 (ApiError) when absent. */
export function getPolicy(policyId: string): Promise<PolicyRecordInfo> {
  return request<PolicyRecordInfo>(`/api/policies/${encodeURIComponent(policyId)}`);
}

/** The updated record after promotion. 404 unknown id; 422 when the basis is
 * blank or the lifecycle transition is not legal (promotion always passes
 * through an evidence-gathering state first). */
export function promotePolicy(
  policyId: string,
  version: number,
  evidenceBasis: string,
  note?: string,
): Promise<PolicyRecordInfo> {
  // `confirm: true` is only ever sent from the confirmation dialog, and the
  // version is always the record the operator is looking at — never a
  // silently-newer registration (M9: promote-latest is a CLI convenience,
  // not a console behavior).
  return post<PolicyRecordInfo>(`/api/policies/${encodeURIComponent(policyId)}/promote`, {
    evidence_basis: evidenceBasis,
    note: note ?? "",
    version,
    confirm: true,
  });
}

// -- Filesystem browse + harness detect (read-only) ----------------------------
// Session-creation aids: directory names and repo marker files only. Every
// result is a DRAFT suggestion — the server re-validates through load_profile
// on creation, so a suggestion can never widen authority.

export interface FsBrowseEntry {
  name: string;
  is_git_worktree: boolean;
  is_hidden: boolean;
}

export interface FsBrowseResult {
  path: string;
  parent: string | null;
  is_git_worktree: boolean;
  entries: FsBrowseEntry[];
  truncated: boolean;
  shortcuts: string[];
  notes: string[];
}

export interface FsDetectedCheck {
  name: string;
  kind: string;
  argv: string[];
  timeout_seconds: number;
}

export interface FsDetectResult {
  is_git_worktree: boolean;
  checks: FsDetectedCheck[];
  required: string[];
  allowed_prefixes: string[];
  notes: string[];
}

/** Subdirectory listing for the repo picker (default: the user's home). */
export function browseDirectories(path?: string): Promise<FsBrowseResult> {
  const query = path === undefined ? "" : `?path=${encodeURIComponent(path)}`;
  return request<FsBrowseResult>(`/api/fs/browse${query}`);
}

/** Test-harness suggestion derived from a repository's marker files. */
export function detectHarness(path: string): Promise<FsDetectResult> {
  return request<FsDetectResult>(`/api/fs/detect?path=${encodeURIComponent(path)}`);
}

export function sessionWsUrl(runId: string): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}/ws/sessions/${encodeURIComponent(runId)}`;
}
